import type { Metadata, Viewport } from "next";

import { ThemeToggle } from "@/components/theme-toggle";

import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "Giveaways",
    template: "%s · Giveaways",
  },
  description:
    "Open-source Discord giveaways with a provably fair, independently verifiable winner draw.",
  robots: {
    // The admin panel must never be indexed; public giveaway pages are fine.
    index: true,
    follow: true,
  },
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#14121a" },
  ],
  width: "device-width",
  initialScale: 1,
};

// Applies the saved theme before first paint to avoid a flash of the wrong one.
const themeScript = `
(function () {
  try {
    var stored = localStorage.getItem('gw-theme');
    var prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    var dark = stored ? stored === 'dark' : prefersDark;
    document.documentElement.classList.toggle('dark', dark);
  } catch (e) {}
})();
`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="flex flex-col">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-[var(--accent)] focus:px-4 focus:py-2 focus:text-[var(--accent-foreground)]"
        >
          Skip to content
        </a>

        <header className="sticky top-0 z-40 border-b border-[var(--border)] bg-[var(--background)]/85 backdrop-blur-md">
          <div className="mx-auto flex h-14 max-w-6xl items-center gap-4 px-4">
            <a href="/" className="flex items-center gap-2 font-semibold">
              <span
                aria-hidden="true"
                className="grid size-7 place-items-center rounded-lg bg-[var(--accent)] text-sm text-[var(--accent-foreground)]"
              >
                🎁
              </span>
              <span>Giveaways</span>
            </a>

            <nav className="ml-2 hidden items-center gap-1 text-sm sm:flex">
              <a
                href="/giveaways"
                className="rounded-md px-3 py-1.5 text-[var(--muted-foreground)] transition-colors hover:bg-[var(--surface-muted)] hover:text-[var(--foreground)]"
              >
                Browse
              </a>
              <a
                href="/admin"
                className="rounded-md px-3 py-1.5 text-[var(--muted-foreground)] transition-colors hover:bg-[var(--surface-muted)] hover:text-[var(--foreground)]"
              >
                Admin
              </a>
            </nav>

            <div className="ml-auto flex items-center gap-2">
              <ThemeToggle />
            </div>
          </div>
        </header>

        <main id="main" className="mx-auto w-full max-w-6xl flex-1 px-4 py-8">
          {children}
        </main>

        <footer className="border-t border-[var(--border)] py-6 text-center text-xs text-[var(--muted-foreground)]">
          <p>
            Open-source giveaway bot · every draw is{" "}
            <a
              href="https://github.com/your-org/giveaway-bot"
              className="underline underline-offset-2 hover:text-[var(--foreground)]"
            >
              independently verifiable
            </a>
          </p>
        </footer>
      </body>
    </html>
  );
}