/**
 * The small set of pieces every screen is built from.
 *
 * Deliberately few and deliberately plain. The product's job is to make a diff,
 * a gap list and a draft email legible enough that a person can say yes to them
 * — the interface competing for attention with the content would be working
 * against the only thing it is for.
 */
"use client";

import Link from "next/link";
import type { ReactNode } from "react";

import type { ApiError } from "@/lib/api";
import { connectGmailUrl, signInUrl } from "@/lib/api";

/* ── surfaces ──────────────────────────────────────────────────────── */

export function Card({
  title,
  aside,
  children,
  tone = "plain",
}: {
  title?: ReactNode;
  aside?: ReactNode;
  children: ReactNode;
  tone?: "plain" | "good" | "warn" | "bad";
}) {
  const border =
    tone === "good"
      ? "var(--good)"
      : tone === "warn"
        ? "var(--warn)"
        : tone === "bad"
          ? "var(--bad)"
          : "var(--line)";
  return (
    <section
      className="rounded-lg border p-4 sm:p-5"
      style={{ background: "var(--surface)", borderColor: border }}
    >
      {(title || aside) && (
        <header className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
          {title && <h2 className="text-base font-semibold">{title}</h2>}
          {aside && (
            <div className="text-xs" style={{ color: "var(--ink-faint)" }}>
              {aside}
            </div>
          )}
        </header>
      )}
      {children}
    </section>
  );
}

export function Muted({ children }: { children: ReactNode }) {
  return (
    <p className="text-sm" style={{ color: "var(--ink-soft)" }}>
      {children}
    </p>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <p
      className="rounded-md border border-dashed px-3 py-6 text-center text-sm"
      style={{ borderColor: "var(--line)", color: "var(--ink-faint)" }}
    >
      {children}
    </p>
  );
}

/* ── controls ──────────────────────────────────────────────────────── */

type ButtonProps = {
  children: ReactNode;
  onClick?: () => void;
  href?: string;
  kind?: "primary" | "plain" | "danger";
  disabled?: boolean;
  busy?: boolean;
  type?: "button" | "submit";
};

export function Button({
  children,
  onClick,
  href,
  kind = "plain",
  disabled,
  busy,
  type = "button",
}: ButtonProps) {
  const base =
    "inline-flex items-center justify-center gap-2 rounded-md border px-3 py-2 " +
    "text-sm font-medium transition-opacity disabled:opacity-50 " +
    "disabled:cursor-not-allowed";
  const style =
    kind === "primary"
      ? { background: "var(--accent)", color: "var(--accent-ink)", borderColor: "var(--accent)" }
      : kind === "danger"
        ? { background: "transparent", color: "var(--bad)", borderColor: "var(--bad)" }
        : { background: "transparent", color: "var(--ink)", borderColor: "var(--line)" };

  const label = busy ? <>{children}…</> : children;

  if (href && !disabled) {
    const external = href.startsWith("http");
    return external ? (
      <a className={base} style={style} href={href}>
        {label}
      </a>
    ) : (
      <Link className={base} style={style} href={href}>
        {label}
      </Link>
    );
  }
  return (
    <button
      type={type}
      className={base}
      style={style}
      onClick={onClick}
      disabled={disabled || busy}
    >
      {label}
    </button>
  );
}

export function Field({
  label,
  value,
  onChange,
  rows,
  hint,
  mono,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  rows?: number;
  hint?: string;
  mono?: boolean;
}) {
  const shared =
    "mt-1 w-full rounded-md border px-3 py-2 text-sm " +
    (mono ? "font-mono " : "");
  const style = { background: "var(--ground)", borderColor: "var(--line)" };
  return (
    <label className="block">
      <span className="text-xs font-medium" style={{ color: "var(--ink-soft)" }}>
        {label}
      </span>
      {rows ? (
        <textarea
          className={shared}
          style={style}
          rows={rows}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <input
          className={shared}
          style={style}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
      {hint && (
        <span className="mt-1 block text-xs" style={{ color: "var(--ink-faint)" }}>
          {hint}
        </span>
      )}
    </label>
  );
}

/* ── status ────────────────────────────────────────────────────────── */

const TONE: Record<string, "good" | "warn" | "bad" | "plain"> = {
  sent: "good",
  approved: "good",
  confirmed: "good",
  needs_review: "warn",
  needs_confirm: "warn",
  sending: "warn",
  tailoring: "warn",
  parsing: "warn",
  uploaded: "plain",
  created: "plain",
  superseded: "plain",
  rejected: "bad",
  failed: "bad",
};

/** Plain words, because a status is the one thing on the screen a person must
 *  not have to interpret. */
const WORDS: Record<string, string> = {
  uploaded: "uploaded",
  parsing: "reading it",
  needs_confirm: "check the parse",
  confirmed: "confirmed",
  superseded: "replaced",
  created: "queued",
  tailoring: "tailoring",
  needs_review: "waiting for you",
  approved: "approved",
  rejected: "rejected",
  sending: "sending",
  sent: "sent",
  failed: "failed",
};

export function Status({ value }: { value: string }) {
  const tone = TONE[value] ?? "plain";
  const colour =
    tone === "good"
      ? { bg: "var(--good-bg)", fg: "var(--good)" }
      : tone === "warn"
        ? { bg: "var(--warn-bg)", fg: "var(--warn)" }
        : tone === "bad"
          ? { bg: "var(--bad-bg)", fg: "var(--bad)" }
          : { bg: "var(--ground)", fg: "var(--ink-soft)" };
  return (
    <span
      className="inline-block rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap"
      style={{ background: colour.bg, color: colour.fg }}
    >
      {WORDS[value] ?? value}
    </span>
  );
}

/* ── errors ────────────────────────────────────────────────────────── */

/**
 * An error, shown as the thing to do about it.
 *
 * The API distinguishes "sign in", "connect Gmail", "this instance is not
 * configured" and "the run moved under you" with different status codes, and
 * each one has a different next step. Collapsing them into one red box would
 * throw away the part that helps.
 */
export function Problem({ error, onRetry }: { error: ApiError; onRetry?: () => void }) {
  const action =
    error.reason === "signin" ? (
      <Button kind="primary" href={signInUrl}>
        Sign in with Google
      </Button>
    ) : error.reason === "connect" ? (
      <Button kind="primary" href={connectGmailUrl}>
        Connect Gmail
      </Button>
    ) : error.reason === "stale" && onRetry ? (
      <Button onClick={onRetry}>Reload</Button>
    ) : onRetry ? (
      <Button onClick={onRetry}>Try again</Button>
    ) : null;

  return (
    <div
      className="rounded-lg border p-4"
      style={{ background: "var(--bad-bg)", borderColor: "var(--bad)" }}
    >
      <p className="text-sm font-medium" style={{ color: "var(--bad)" }}>
        {error.reason === "setup"
          ? "This instance is not set up for that yet"
          : error.reason === "offline"
            ? "The API is not answering"
            : "That did not work"}
      </p>
      <p className="mt-1 text-sm" style={{ color: "var(--ink)" }}>
        {error.message}
      </p>
      {action && <div className="mt-3">{action}</div>}
    </div>
  );
}

export function Loading({ what }: { what: string }) {
  return (
    <p className="text-sm" style={{ color: "var(--ink-faint)" }}>
      Loading {what}…
    </p>
  );
}
