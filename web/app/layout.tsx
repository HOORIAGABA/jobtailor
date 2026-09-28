import type { Metadata, Viewport } from "next";

import "./globals.css";
import { Nav } from "@/components/nav";

export const metadata: Metadata = {
  title: {
    default: "JobTailor — résumé tailoring with a paper trail",
    template: "%s · JobTailor",
  },
  description:
    "Tailors a résumé to a posting, drafts the outreach, and refuses anything " +
    "it cannot trace to a line you actually wrote.",
};

export const viewport: Viewport = {
  // Both, so the sticky bar and the page ground match the browser chrome in
  // either theme instead of leaving a pale strip above a dark page.
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#fbf8f1" },
    { media: "(prefers-color-scheme: dark)", color: "#12110f" },
  ],
};

/**
 * Applies the saved theme before the first paint.
 *
 * Without this, someone who has chosen dark on a machine set to light sees a
 * white flash on every navigation: the choice lives in `localStorage`, React
 * cannot read it during server rendering, and by the time an effect runs the page
 * has already been painted. A blocking script in `<head>` is the one place that
 * runs early enough. It is wrapped in try/catch because storage access throws
 * outright in a private window with site data blocked, and if it fails the system
 * preference simply wins — which is the right fallback.
 */
const THEME_SCRIPT = `
try {
  var t = localStorage.getItem("theme");
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
} catch (e) {}
`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
        {/*
          A plain <link> rather than `next/font`. `next/font` downloads the faces
          at build time, which turns a font CDN outage into a failed CI build —
          and this project's CI runs on every push to a portfolio repo. The
          tradeoff is one extra connection at runtime, mitigated by preconnect,
          and `display=swap` so text is never invisible while waiting.
        */}
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link
          rel="preconnect"
          href="https://fonts.gstatic.com"
          crossOrigin="anonymous"
        />
        <link
          rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700;800&family=Newsreader:opsz,wght@6..72,400;6..72,500&family=JetBrains+Mono:wght@400;500;700&display=swap"
        />
      </head>
      <body className="flex min-h-dvh flex-col">
        <Nav />
        <main className="flex-1">{children}</main>
        <footer className="border-t border-line">
          <div className="mx-auto flex w-full max-w-5xl flex-wrap items-center justify-between gap-x-6 gap-y-2 px-4 py-6 text-sm text-ink-faint">
            <p className="max-w-lg font-serif">
              Nothing is sent without your approval. Every claim in a tailored
              résumé traces back to a line in the one you uploaded.
            </p>
            <a
              href="https://github.com/HOORIAGABA/jobtailor"
              className="transition-colors hover:text-ink"
            >
              Source
            </a>
          </div>
        </footer>
      </body>
    </html>
  );
}
