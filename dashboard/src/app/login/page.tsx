import Link from "next/link";

import { discordConfigured } from "@/lib/auth";
import { sessionsAvailable } from "@/lib/session";

export const dynamic = "force-dynamic";

const ERROR_MESSAGES: Record<string, string> = {
  discord_denied: "Sign-in was cancelled in Discord.",
  missing_code: "Discord did not return an authorization code.",
  invalid_state: "That sign-in link expired or was already used. Please try again.",
  oauth_not_configured: "Discord OAuth2 is not configured on this deployment.",
  server_misconfigured: "This deployment is missing its SESSION_SECRET.",
  exchange_failed: "Could not complete sign-in with Discord. Please try again.",
};

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; redirect_to?: string }>;
}) {
  const { error, redirect_to } = await searchParams;
  const message = error ? (ERROR_MESSAGES[error] ?? "Sign-in failed.") : null;
  const ready = sessionsAvailable() && discordConfigured();

  return (
    <div className="mx-auto flex min-h-[60vh] max-w-md flex-col justify-center">
      <div className="animate-scale-in rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-8 text-center">
        <span aria-hidden="true" className="text-4xl">🔐</span>
        <h1 className="mt-4 text-2xl font-semibold">Sign in</h1>
        <p className="mt-2 text-sm text-[var(--muted-foreground)]">
          Sign in with Discord to manage giveaways. You will only be able to see servers where you
          have Manage Server permission.
        </p>

        {message && (
          <p
            role="alert"
            className="mt-4 rounded-lg bg-[var(--danger)]/12 px-3 py-2 text-sm text-[var(--danger)]"
          >
            {message}
          </p>
        )}

        {!ready ? (
          <div className="mt-6 rounded-lg bg-[var(--warning)]/12 px-3 py-2 text-left text-xs text-[var(--warning)]">
            <p className="font-medium">This deployment is not fully configured.</p>
            <p className="mt-1">
              Set <code className="font-mono">SESSION_SECRET</code> (32+ characters),{" "}
              <code className="font-mono">DISCORD_CLIENT_ID</code>,{" "}
              <code className="font-mono">DISCORD_CLIENT_SECRET</code> and{" "}
              <code className="font-mono">DISCORD_REDIRECT_URI</code>, then redeploy.
            </p>
          </div>
        ) : (
          <a
            href={redirect_to ? `/api/auth/login?redirect_to=${encodeURIComponent(redirect_to)}` : "/api/auth/login"}
            className="mt-6 flex items-center justify-center gap-2 rounded-lg bg-[#5865F2] px-5 py-2.5 font-medium text-white transition-transform hover:scale-[1.02]"
          >
            <span aria-hidden="true">💬</span>
            Continue with Discord
          </a>
        )}

        <p className="mt-6 text-xs text-[var(--muted-foreground)]">
          Public giveaway pages need no sign-in.{" "}
          <Link href="/giveaways" className="underline underline-offset-2">
            Browse them instead
          </Link>
          .
        </p>
      </div>
    </div>
  );
}