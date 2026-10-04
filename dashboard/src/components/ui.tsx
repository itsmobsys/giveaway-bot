import type { ReactNode } from "react";

/** Card container with an optional title, description and id (for anchors). */
export function Card({
  title,
  description,
  action,
  id,
  children,
  className = "",
}: {
  title?: string;
  description?: string;
  action?: ReactNode;
  id?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      id={id}
      className={`rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] ${className}`}
    >
      {(title || action) && (
        <header className="flex items-start justify-between gap-4 border-b border-[var(--border)] px-5 py-4">
          <div>
            {title && <h2 className="font-semibold">{title}</h2>}
            {description && (
              <p className="mt-0.5 text-sm text-[var(--muted-foreground)]">{description}</p>
            )}
          </div>
          {action}
        </header>
      )}
      <div className="p-5">{children}</div>
    </section>
  );
}

const STATUS_STYLES: Record<string, { label: string; className: string }> = {
  running: { label: "Running", className: "bg-[var(--accent)]/12 text-[var(--accent)]" },
  paused: { label: "Paused", className: "bg-[var(--warning)]/14 text-[var(--warning)]" },
  ended: { label: "Ended", className: "bg-[var(--success)]/14 text-[var(--success)]" },
  scheduled: { label: "Scheduled", className: "bg-[var(--info)]/14 text-[var(--info)]" },
};

/** Status pill. Colour is paired with text, never colour alone. */
export function StatusBadge({ status }: { status: string }) {
  const style = STATUS_STYLES[status] ?? {
    label: status,
    className: "bg-[var(--surface-muted)] text-[var(--muted-foreground)]",
  };
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${style.className}`}
    >
      {status === "running" && (
        <span
          aria-hidden="true"
          className="size-1.5 animate-pulse rounded-full bg-current"
        />
      )}
      {style.label}
    </span>
  );
}

/** Small labelled metric. */
export function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: ReactNode;
  hint?: string;
}) {
  return (
    <div className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4">
      <p className="text-xs font-medium uppercase tracking-wide text-[var(--muted-foreground)]">
        {label}
      </p>
      <p className="tabular mt-1 text-2xl font-semibold">{value}</p>
      {hint && <p className="mt-0.5 text-xs text-[var(--muted-foreground)]">{hint}</p>}
    </div>
  );
}

/** Consistent empty state so no list ever renders as blank space. */
export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon: string;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-2 px-6 py-12 text-center">
      <span aria-hidden="true" className="text-3xl opacity-70">
        {icon}
      </span>
      <p className="font-medium">{title}</p>
      <p className="max-w-sm text-sm text-[var(--muted-foreground)]">{description}</p>
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

/** Loading placeholder with matching shape to the content it replaces. */
export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`skeleton rounded-md ${className}`} aria-hidden="true" />;
}

export function LoadingBlock({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-3" role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className="h-16 w-full" />
      ))}
      <span className="sr-only">Loading…</span>
    </div>
  );
}