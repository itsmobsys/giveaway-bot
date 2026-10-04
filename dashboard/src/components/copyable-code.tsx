"use client";

/**
 * Monospace value with a copy-to-clipboard button.
 *
 * Lives in its own client module because it has an onClick handler, and it is
 * rendered from server components - importing it creates the boundary.
 */

import { useState } from "react";

export function CopyableCode({
  value,
  label,
  truncate = 24,
}: {
  value: string;
  label?: string;
  truncate?: number;
}) {
  const [copied, setCopied] = useState(false);
  const shown = value.length > truncate ? `${value.slice(0, truncate)}…` : value;

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard blocked (insecure context or permissions): the full value is
      // still available via the title attribute.
    }
  }

  return (
    <div className="flex items-center gap-2">
      {label && <span className="text-sm text-[var(--muted-foreground)]">{label}</span>}
      <code className="rounded bg-[var(--surface-muted)] px-2 py-1 font-mono text-xs" title={value}>
        {shown}
      </code>
      <button
        type="button"
        onClick={copy}
        aria-label="Copy to clipboard"
        className="rounded border border-[var(--border)] px-2 py-1 text-xs transition-colors hover:bg-[var(--surface-muted)]"
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}