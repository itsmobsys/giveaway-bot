//! Port of the retired Python `RequestTimeTests` cases.
//!
//! The Python cases drove `/gw_request_time` through `tree.get_command(...)` and
//! read the text off a fake interaction's followup. serenity 0.12 builds neither
//! a `Context` nor a `CommandInteraction` outside its own dispatch, so every case
//! here calls the handler's two halves directly with the arguments it passes:
//! [`measure_probe`] for the live legs, then [`render_report`] with the ack, REST
//! and loop-lag numbers as plain `Option<f64>`.

use super::*;

/// The report `/gw_request_time` renders, from the bot's real cache.
///
/// The three Discord numbers are supplied by hand: the ack is the defer the
/// handler timed, the reply is one real REST send, and the loop lag is a yield
/// through the runtime. They are exactly what the handler passes in.
async fn report_for(f: &BotFixture) -> String {
    let measured = measure_probe(&f.bot, GUILD, CHANNEL, CREATED_BY).await;
    render_report(&measured, Some(0.4), Some(12.0), Some(0.02), 20.0)
}

/// The probe must name every leg it measures, and no database leg at all.
#[tokio::test(flavor = "multi_thread")]
async fn breakdown_names_every_live_leg() {
    // ADAPTED: `request_time` needs a `Context`, so the handler's own call pair
    // (`measure_probe` + `render_report`) is what runs here.
    let f = BotFixture::new().await;
    f.seed_notify_role().await;
    f.make().await;

    let text = report_for(&f).await;

    for leg in [
        "Discord ack (defer)",
        "Discord reply (REST send)",
        "Redis ping",
        "Redis cache count",
        "Redis cache list",
        "Redis cache notify-role",
        "Redis write",
        "Redis ops this command",
        "Warm cache legs",
        "Gateway heartbeat",
        "Total measured",
        "Slowest leg",
    ] {
        assert!(text.contains(leg), "missing leg {leg:?}: {text}");
    }
    // The database is not this command's business any more: not one of its
    // legs, not its slowest, not its total. A cache miss or a Redis outage
    // falls through to the database, and that cost belongs to another report.
    assert!(!text.contains("Turso"), "{text}");
    assert!(
        !text
            .replace("No database leg is measured or printed", "")
            .contains("database leg is measured or printed"),
        "the only mention of a database leg must be the negated footnote: {text}"
    );
}

/// No cache configured must read as absent, never as a fast zero.
#[tokio::test(flavor = "multi_thread")]
async fn probe_says_so_when_there_is_no_redis() {
    // ADAPTED: Python's fixture had no cache wired in; `without_cache()` is the
    // same bot, built from an empty `REDIS_URL`.
    let f = BotFixture::without_cache().await;

    let text = report_for(&f).await;

    assert!(text.contains("no REDIS_URL"), "{text}");
    assert!(text.contains("not configured"), "{text}");
    // A cache that is off never served anything, so the report may not claim a
    // hit anywhere on it.
    assert!(!text.contains("HIT"), "{text}");
}

/// The write probe is Redis-only: no probe row may be written.
#[tokio::test(flavor = "multi_thread")]
async fn write_probe_leaves_no_row_behind() {
    // ADAPTED: the command is driven as `measure_probe` + `render_report`; the
    // row check is the same SQL the Python case ran against the same table.
    let f = BotFixture::new().await;
    let _text = report_for(&f).await;

    let rows = f
        .bot
        .service()
        .db()
        .query(
            "SELECT * FROM simple_message_counts WHERE user_id = ?",
            &["gw_request_time_probe".into()],
        )
        .await
        .expect("probe row query");
    assert!(
        rows.is_empty(),
        "the probe must not leave a message-count row: {} row(s)",
        rows.len()
    );
}

/// The lookup is timed, the write is split out, and an unmeasured leg says so.
#[tokio::test(flavor = "multi_thread")]
async fn probe_times_lookup_and_splits_write() {
    // ADAPTED: Python's bot for this case had no cache, which is what makes the
    // list leg report "not measured" instead of borrowing a number.
    let f = BotFixture::without_cache().await;

    let text = report_for(&f).await;

    for leg in ["Event-loop lag", "Redis cache list"] {
        assert!(text.contains(leg), "missing leg {leg:?}: {text}");
    }
    // With no cache wired in, the list leg must say it was not measured instead
    // of borrowing a number from somewhere else.
    assert!(text.contains("not measured"), "{text}");
    let rows = f
        .bot
        .service()
        .db()
        .query(
            "SELECT * FROM simple_message_counts WHERE user_id = ?",
            &["gw_request_time_probe".into()],
        )
        .await
        .expect("probe row query");
    assert!(
        rows.is_empty(),
        "the write probe is Redis-only: {} row(s) were written",
        rows.len()
    );
    assert!(!text.contains("Turso"), "{text}");
}

/// A DM is refused before any probe work happens.
#[test]
fn dm_says_to_use_a_server() {
    // ADAPTED: the reply and the early return are asserted on the handler's own
    // source. Python called the callback with `interaction.guild = None` and read
    // `interaction.response.sent[0]`; a serenity handler cannot be invoked that
    // way, and the guard is the part worth pinning.
    assert_eq!(USE_IN_SERVER, "Use this in a server.");

    let body = body_of(COMMANDS_ADMIN_SOURCE, "async fn request_time");
    let guard = body
        .find("ci.guild_id")
        .unwrap_or_else(|| panic!("no guild guard in the handler: {body}"));
    let reply = body
        .find("reply_ephemeral(ctx, ci, USE_IN_SERVER)")
        .unwrap_or_else(|| panic!("the DM branch must reply with USE_IN_SERVER: {body}"));
    let probe = body
        .find("measure_probe")
        .unwrap_or_else(|| panic!("the handler must run the probe: {body}"));

    assert!(
        guard < reply,
        "the reply belongs to the guild guard: {body}"
    );
    // Everything between the guard and the probe is the DM branch, so it must
    // answer and stop: no probe work, and no followup behind the first reply
    // (`interaction.followup.sent == []` in Python).
    let guard_to_probe = &body[guard..probe];
    assert!(
        guard_to_probe.contains("return Ok(())"),
        "the DM branch must answer and return before the probe: {guard_to_probe}"
    );
    assert!(
        !guard_to_probe.contains("create_followup") && !guard_to_probe.contains("safe_followup"),
        "a DM must get exactly one reply, not a followup too: {guard_to_probe}"
    );
    assert!(
        reply < probe,
        "the DM reply must precede every measured leg: {body}"
    );
}
