//! Tiny HTTP server so the bot can run as a Render Web Service.
//!
//! Render's free tier has no background workers, and a Web Service deploy times
//! out ("no open ports detected") unless the process binds a port. This binds
//! `PORT` and answers `/`, `/health` and `/ready`; nothing else. It runs as a
//! spawned task on the same runtime, so it can never block the Discord loop.
//!
//! `/health` is liveness (process alive — always 200); `/ready` is readiness
//! (Discord gateway up — 200 only when online, else 503). Splitting them stops
//! a green `/health` from falsely implying the bot is operational while a login
//! throttle cools down. Render's health check path stays `/health`, so a 429
//! never triggers a container restart.

use std::net::SocketAddr;
use std::sync::Arc;

use anyhow::{Context, Result};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{watch, RwLock};

/// Paths answered with 200 (plus `/ready`'s readiness logic). Everything else
/// 404s. A query string is ignored, so `/health?cachebust=1` still works.
pub const OK_PATHS: &[&str] = &["/", "/health", "/ready"];

/// Mutable readiness state, shared with the bot loop.
#[derive(Debug, Clone)]
pub struct HealthState {
    /// "starting", "online", "retrying-N".
    pub status: String,
    /// "starting", "online", "rate-limited", "auth-failed", "stopped".
    pub discord: String,
}

impl Default for HealthState {
    fn default() -> Self {
        Self {
            status: "starting".to_string(),
            discord: "starting".to_string(),
        }
    }
}

/// A running health server. Dropping it does not stop the task; call
/// [`HealthServer::shutdown`] for that (used by tests).
pub struct HealthServer {
    pub addr: SocketAddr,
    state: Arc<RwLock<HealthState>>,
    shutdown: watch::Sender<bool>,
}

impl HealthServer {
    /// Shared readiness state, for the bot to update as the gateway changes.
    pub fn state(&self) -> Arc<RwLock<HealthState>> {
        Arc::clone(&self.state)
    }

    /// Ask the accept loop to stop. Idempotent.
    pub fn shutdown(&self) {
        let _ = self.shutdown.send(true);
    }
}

/// Bind `0.0.0.0:port` and start serving. Returns once the socket is listening.
pub async fn start_health_server(
    port: u16,
    state: Arc<RwLock<HealthState>>,
) -> Result<HealthServer> {
    // Render requires binding all interfaces; a loopback-only bind is invisible
    // to its proxy and fails the deploy health check.
    let listener = TcpListener::bind(("0.0.0.0", port))
        .await
        .with_context(|| format!("could not bind the health server to port {port}"))?;
    let mut addr = listener.local_addr()?;
    if addr.ip().is_unspecified() {
        // Windows (and most stacks) refuse to *connect* to 0.0.0.0, so the
        // address handed back is the loopback one. The socket itself stays
        // bound to every interface, which is what Render needs.
        addr.set_ip(std::net::IpAddr::V4(std::net::Ipv4Addr::LOCALHOST));
    }
    let (shutdown, mut shutdown_rx) = watch::channel(false);
    let task_state = Arc::clone(&state);
    tokio::spawn(async move {
        loop {
            tokio::select! {
                _ = shutdown_rx.changed() => {
                    if *shutdown_rx.borrow() {
                        break;
                    }
                }
                accepted = listener.accept() => {
                    let Ok((stream, _peer)) = accepted else { continue };
                    let state = Arc::clone(&task_state);
                    tokio::spawn(async move {
                        // A monitor that hangs up mid-response is routine; only
                        // a genuine fault is worth a log line.
                        if let Err(e) = serve_connection(stream, state).await {
                            tracing::debug!("health connection dropped: {e}");
                        }
                    });
                }
            }
        }
    });
    tracing::info!("health server listening on port {port}");
    Ok(HealthServer {
        addr,
        state,
        shutdown,
    })
}

async fn serve_connection(mut stream: TcpStream, state: Arc<RwLock<HealthState>>) -> Result<()> {
    let mut buffer = Vec::with_capacity(1024);
    loop {
        // Read until the end of the request headers. A keep-alive client can
        // send several requests down one connection, so the buffer is drained
        // request by request.
        let mut chunk = [0u8; 1024];
        let header_end = loop {
            if let Some(pos) = find_headers_end(&buffer) {
                break pos;
            }
            let read = stream.read(&mut chunk).await?;
            if read == 0 {
                return Ok(());
            }
            buffer.extend_from_slice(&chunk[..read]);
            if buffer.len() > 16 * 1024 {
                // A request that never ends is not a health check.
                return Ok(());
            }
        };

        let head = String::from_utf8_lossy(&buffer[..header_end]).to_string();
        buffer.drain(..header_end + 4);
        let (method, path) = match parse_request_line(&head) {
            Some(parsed) => parsed,
            None => return Ok(()),
        };

        let response = build_response(&method, &path, &state).await;
        stream.write_all(response.as_bytes()).await?;
        stream.flush().await?;
        // Python's handler is HTTP/1.1 keep-alive; honour it, but never wait
        // forever for a client that has nothing more to say.
        if !head.to_ascii_lowercase().contains("keep-alive") {
            return Ok(());
        }
    }
}

fn find_headers_end(buffer: &[u8]) -> Option<usize> {
    buffer.windows(4).position(|w| w == b"\r\n\r\n")
}

/// `GET /health?x=1 HTTP/1.1` -> `("GET", "/health")`.
fn parse_request_line(head: &str) -> Option<(String, String)> {
    let mut parts = head.lines().next()?.split_whitespace();
    let method = parts.next()?.to_string();
    let target = parts.next()?;
    let path = target.split('?').next().unwrap_or(target).to_string();
    Some((method, path))
}

async fn build_response(method: &str, path: &str, state: &Arc<RwLock<HealthState>>) -> String {
    let head_only = method.eq_ignore_ascii_case("HEAD");
    let get = method.eq_ignore_ascii_case("GET");
    if !head_only && !get {
        return response(404, "not found", false);
    }

    match path {
        "/ready" => {
            let discord = state.read().await.discord.clone();
            if discord == "online" {
                response(200, "ready: discord online", head_only)
            } else {
                response(503, &format!("not ready: discord {discord}"), head_only)
            }
        }
        "/" => {
            let snapshot = state.read().await.clone();
            let online = snapshot.status == "online";
            // Kept byte-for-byte identical to the Python status page, including
            // its trailing parenthesis.
            let body = format!(
                "giveaway-bot: {} discord={} {} — Discord commands appear once the gateway connects)",
                if online {
                    "online — gateway connected".to_string()
                } else {
                    snapshot.status
                },
                snapshot.discord,
                if online { "(ready)" } else { "(bot not ready" },
            );
            response(200, &body, head_only)
        }
        "/health" => response(200, "ok", head_only),
        _ => response(404, "not found", head_only),
    }
}

fn response(status: u16, body: &str, head_only: bool) -> String {
    let reason = match status {
        200 => "OK",
        404 => "Not Found",
        503 => "Service Unavailable",
        _ => "OK",
    };
    let payload = if head_only { "" } else { body };
    format!(
        "HTTP/1.1 {status} {reason}\r\n\
         Content-Type: text/plain; charset=utf-8\r\n\
         Content-Length: {}\r\n\
         Connection: keep-alive\r\n\
         \r\n\
         {payload}",
        payload.len()
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    async fn spawn() -> (HealthServer, Arc<RwLock<HealthState>>) {
        // Port 0 lets the OS pick a free port; tests must never collide with a
        // real bot or with each other.
        let state = Arc::new(RwLock::new(HealthState::default()));
        let server = start_health_server(0, Arc::clone(&state)).await.unwrap();
        (server, state)
    }

    async fn request(addr: SocketAddr, method: &str, path: &str) -> (u16, String) {
        let mut stream = TcpStream::connect(addr).await.unwrap();
        stream
            .write_all(
                format!("{method} {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                    .as_bytes(),
            )
            .await
            .unwrap();
        let mut raw = String::new();
        stream.read_to_string(&mut raw).await.unwrap();
        let status: u16 = raw
            .split_whitespace()
            .nth(1)
            .and_then(|s| s.parse().ok())
            .unwrap_or(0);
        let body = raw.split("\r\n\r\n").nth(1).unwrap_or("").to_string();
        (status, body)
    }

    #[tokio::test]
    async fn health_is_liveness_and_stays_200_while_starting() {
        let (server, _state) = spawn().await;
        let (status, body) = request(server.addr, "GET", "/health").await;
        assert_eq!(status, 200);
        assert_eq!(body, "ok");
        server.shutdown();
    }

    #[tokio::test]
    async fn ready_is_503_until_the_gateway_is_online() {
        let (server, state) = spawn().await;
        let (status, body) = request(server.addr, "GET", "/ready").await;
        assert_eq!(status, 503);
        assert_eq!(body, "not ready: discord starting");

        {
            let mut guard = state.write().await;
            guard.discord = "online".to_string();
            guard.status = "online".to_string();
        }
        let (status, body) = request(server.addr, "GET", "/ready").await;
        assert_eq!(status, 200);
        assert_eq!(body, "ready: discord online");
        server.shutdown();
    }

    #[tokio::test]
    async fn root_reports_both_states() {
        let (server, state) = spawn().await;
        let (status, body) = request(server.addr, "GET", "/").await;
        assert_eq!(status, 200);
        assert_eq!(
            body,
            "giveaway-bot: starting discord=starting (bot not ready — Discord commands appear once the gateway connects)"
        );
        {
            let mut guard = state.write().await;
            guard.discord = "online".to_string();
            guard.status = "online".to_string();
        }
        let (_, body) = request(server.addr, "GET", "/").await;
        assert_eq!(
            body,
            "giveaway-bot: online — gateway connected discord=online (ready) — Discord commands appear once the gateway connects)"
        );
        server.shutdown();
    }

    #[tokio::test]
    async fn query_strings_are_ignored_and_unknown_paths_404() {
        let (server, _state) = spawn().await;
        let (status, _) = request(server.addr, "GET", "/health?cachebust=1").await;
        assert_eq!(status, 200);
        let (status, body) = request(server.addr, "GET", "/nope").await;
        assert_eq!(status, 404);
        assert_eq!(body, "not found");
        server.shutdown();
    }

    #[tokio::test]
    async fn head_probes_get_a_status_and_no_body() {
        let (server, _state) = spawn().await;
        let (status, body) = request(server.addr, "HEAD", "/health").await;
        assert_eq!(status, 200);
        assert_eq!(body, "");
        let (status, _) = request(server.addr, "HEAD", "/ready").await;
        assert_eq!(status, 503);
        server.shutdown();
    }
}
