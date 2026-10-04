"use client";

import { useState } from "react";

export function SignOutButton() {
  const [busy, setBusy] = useState(false);

  return (
    <form action="/api/auth/logout" method="post">
      <button
        type="submit"
        disabled={busy}
        onClick={() => setBusy(true)}
        className="rounded-lg border border-[var(--border)] px-3.5 py-2 text-sm font-medium transition-colors hover:bg-[var(--surface-muted)] disabled:opacity-50"
      >
        {busy ? "Signing out…" : "Sign out"}
      </button>
    </form>
  );
}