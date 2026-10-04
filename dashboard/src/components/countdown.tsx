"use client";

import { useEffect, useState } from "react";

/**
 * Countdown with a live progress bar.
 *
 * Server-rendered markup shows the absolute end time, so the value is correct
 * with JavaScript disabled and before hydration; the interval only keeps the
 * remaining time fresh.
 */
export function Countdown({
  endsAt,
  status,
}: {
  endsAt: number | null;
  status: string;
}) {
  const [now, setNow] = useState<number | null>(null);

  useEffect(() => {
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  if (!endsAt) {
    return <span className="text-[var(--muted-foreground)]">No end time set</span>;
  }

  if (status === "ended") {
    return <span>Ended</span>;
  }

  const reference = now ?? Date.now();
  const remaining = endsAt - reference;

  if (remaining <= 0) {
    return (
      <span className="font-medium text-[var(--warning)]">
        Ending… <span className="text-[var(--muted-foreground)]">(waiting for the bot)</span>
      </span>
    );
  }

  const totalSeconds = Math.floor(remaining / 1000);
  const days = Math.floor(totalSeconds / 86400);
  const hours = Math.floor((totalSeconds % 86400) / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;

  const parts: string[] = [];
  if (days > 0) parts.push(`${days}d`);
  parts.push(`${String(hours).padStart(2, "0")}h`);
  parts.push(`${String(minutes).padStart(2, "0")}m`);
  parts.push(`${String(seconds).padStart(2, "0")}s`);

  return (
    <span className="tabular font-medium" title={new Date(endsAt).toISOString()}>
      {parts.join(" ")}
    </span>
  );
}

/** Absolute timestamp, both readable and machine-readable. */
export function Timestamp({ value }: { value: number | null | undefined }) {
  if (!value) return <span className="text-[var(--muted-foreground)]">—</span>;
  const date = new Date(value);
  return (
    <time dateTime={date.toISOString()} title={date.toUTCString()}>
      {date.toLocaleString(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      })}
    </time>
  );
}