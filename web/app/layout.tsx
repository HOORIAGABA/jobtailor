import type { Metadata } from "next";

import "./globals.css";
import { Nav } from "@/components/nav";

export const metadata: Metadata = {
  title: "JobTailor",
  description:
    "Tailors a resume to a posting, drafts the outreach, and sends nothing " +
    "until you have read it.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-dvh">
        <Nav />
        <main className="mx-auto w-full max-w-4xl px-4 py-6 sm:py-10">
          {children}
        </main>
        <footer
          className="mx-auto w-full max-w-4xl px-4 pb-10 text-xs"
          style={{ color: "var(--ink-faint)" }}
        >
          Nothing is sent without your approval. Every claim in a tailored
          resume traces back to a line in the one you uploaded.
        </footer>
      </body>
    </html>
  );
}
