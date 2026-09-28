/**
 * The pieces every screen is built from.
 *
 * Still a small set, and still subordinate to the content: the product's job is
 * to make a diff, a gap list and a draft email legible enough that a person can
 * say yes to them, and an interface competing for attention with that content
 * would be working against the only thing it is for.
 *
 * Two rules from the design system show up throughout this file:
 *
 *   **Prose is serif, chrome is sans.** Anything a person reads as a sentence —
 *   a lede, a rationale, an explanation — takes `font-serif`. Buttons, labels,
 *   statuses and headings stay sans. That inversion is the identity.
 *
 *   **Stamp red means refused, and nothing else.** `tone="bad"` and the `Stamp`
 *   component are the only things allowed it. A second use would cost the first
 *   one its meaning.
 *
 * Colours come from the tokens in `globals.css` through Tailwind utilities
 * (`bg-surface`, `text-ink-soft`), not from inline styles. One consequence worth
 * knowing: dark mode needs no `dark:` variants anywhere in this file.
 */
"use client";

import Link from "next/link";
import type { ReactNode } from "react";

import type { ApiError } from "@/lib/api";
import { connectGmailUrl, signInUrl } from "@/lib/api";
import { Alert, Arrow, Check, Cross } from "@/components/icons";

/* ── page furniture ────────────────────────────────────────────────────── */

/** The measure for every screen inside the app. The landing page sets its own,
 *  because full-bleed bands are part of its design and not of the app's. */
export function Page({ children }: { children: ReactNode }) {
  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-8 sm:py-12">{children}</div>
  );
}

/**
 * The top of a screen: where you are, what it is, what you can do about it.
 *
 * A separate component because the three pages that have one were each doing it
 * slightly differently — different heading size, different gap, `back` as a link
 * on one and a button on another. Inconsistency at the top of the page is the
 * most visible kind.
 */
export function PageHeader({
  eyebrow,
  title,
  lead,
  actions,
  back,
}: {
  eyebrow?: ReactNode;
  title: ReactNode;
  lead?: ReactNode;
  actions?: ReactNode;
  back?: { href: string; label: string };
}) {
  return (
    <header className="mb-6 sm:mb-8">
      {back && (
        <Link
          href={back.href}
          className="mb-3 inline-flex items-center gap-1.5 text-sm text-ink-faint transition-colors hover:text-ink"
        >
          <Arrow size={14} className="rotate-180" />
          {back.label}
        </Link>
      )}
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="min-w-0">
          {eyebrow && <p className="eyebrow mb-1.5">{eyebrow}</p>}
          <h1 className="text-2xl font-bold text-balance sm:text-3xl">{title}</h1>
          {lead && (
            <p className="mt-2.5 max-w-2xl font-serif text-[17px]/7 text-ink-soft">
              {lead}
            </p>
          )}
        </div>
        {actions && (
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            {actions}
          </div>
        )}
      </div>
    </header>
  );
}

/* ── surfaces ──────────────────────────────────────────────────────────── */

const CARD_TONE = {
  plain: "border-line bg-surface",
  good: "border-good-line bg-surface",
  warn: "border-warn-line bg-surface",
  bad: "border-bad-line bg-surface",
} as const;

/**
 * A tone tints the *edge*, never the fill.
 *
 * The first version filled a warning card with warning-coloured background, and
 * on the gate screen — where the whole page is one decision — that made the most
 * important card the hardest to read text on. A 1px coloured edge plus a
 * coloured header rule carries the same signal and leaves the content on paper.
 */
export function Card({
  title,
  aside,
  children,
  tone = "plain",
  footer,
  id,
}: {
  title?: ReactNode;
  aside?: ReactNode;
  children: ReactNode;
  tone?: keyof typeof CARD_TONE;
  footer?: ReactNode;
  id?: string;
}) {
  return (
    <section
      id={id}
      className={`overflow-hidden rounded-xl border shadow-card ${CARD_TONE[tone]}`}
    >
      {(title || aside) && (
        <header className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-b border-line-soft bg-sunken/60 px-4 py-2.5 sm:px-5">
          {title && <h2 className="text-[13px] font-bold">{title}</h2>}
          {aside && <div className="text-xs text-ink-faint">{aside}</div>}
        </header>
      )}
      <div className="px-4 py-4 sm:px-5">{children}</div>
      {footer && (
        <footer className="border-t border-line-soft bg-sunken/60 px-4 py-3 sm:px-5">
          {footer}
        </footer>
      )}
    </section>
  );
}

/** A row in a list of things you can open. The whole row is the target. */
export function Row({
  href,
  title,
  meta,
  badge,
  action,
}: {
  href?: string;
  title: ReactNode;
  meta?: ReactNode;
  badge?: ReactNode;
  action?: ReactNode;
}) {
  const body = (
    <>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="truncate text-sm font-semibold">{title}</span>
          {badge}
        </div>
        {meta && <div className="mt-1 text-xs text-ink-faint">{meta}</div>}
      </div>
      {action ?? (href && <Arrow size={16} className="shrink-0 text-ink-faint" />)}
    </>
  );

  const shell =
    "flex items-center gap-3 border-b border-line-soft px-4 py-3 last:border-0 sm:px-5";

  if (href && !action) {
    return (
      <Link
        href={href}
        className={`${shell} -mx-4 transition-colors hover:bg-sunken sm:-mx-5`}
      >
        {body}
      </Link>
    );
  }
  return <div className={`${shell} -mx-4 sm:-mx-5`}>{body}</div>;
}

/** Prose. Serif, because it is read as sentences rather than scanned. */
export function Muted({ children }: { children: ReactNode }) {
  return <p className="font-serif text-[16px]/7 text-ink-soft">{children}</p>;
}

export function Empty({
  children,
  action,
}: {
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="rounded-lg border border-dashed border-line px-4 py-10 text-center">
      <p className="mx-auto max-w-md font-serif text-[16px]/7 text-ink-faint">
        {children}
      </p>
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

/** A number worth looking at, with its unit attached. */
export function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-lg border border-line-soft bg-sunken px-3 py-2">
      <div className="text-xl leading-tight font-bold">{value}</div>
      <div className="mt-0.5 text-xs text-ink-faint">{label}</div>
    </div>
  );
}

/**
 * ★ The stamp.
 *
 * The one deliberately loud element in the product. It marks a refused
 * operation, which is the thing this whole system exists to be able to do — so
 * it gets a proofreader's mark rather than a polite grey label. Rotated, ruled,
 * stamp red.
 *
 * It appears in exactly one place in the app: over a rejection on the gate. If a
 * second use is ever added, this one stops meaning anything.
 */
export function Stamp({ children = "Refused" }: { children?: ReactNode }) {
  return <span className="stamp">{children}</span>;
}

/* ── controls ──────────────────────────────────────────────────────────── */

const KIND = {
  /* Ink, not blue, for the primary action — the page is a document and the
     strongest thing on it should be the darkest, not the most saturated. It
     turns pen-blue on hover, which is the one place the accent gets to move. */
  primary:
    "border-transparent bg-ink text-ground shadow-card hover:bg-accent active:translate-y-px",
  plain:
    "border-line bg-surface text-ink shadow-card hover:bg-sunken active:translate-y-px",
  quiet:
    "border-transparent bg-transparent text-ink-soft hover:bg-sunken hover:text-ink",
  danger:
    "border-bad-line bg-surface text-bad shadow-card hover:bg-bad-bg active:translate-y-px",
} as const;

const SIZE = {
  sm: "h-8 gap-1.5 px-2.5 text-xs",
  md: "h-9.5 gap-2 px-3.5 text-sm",
  lg: "h-11 gap-2 px-5 text-[15px]",
} as const;

export function Button({
  children,
  onClick,
  href,
  kind = "plain",
  size = "md",
  disabled,
  busy,
  type = "button",
  icon,
  title,
  full,
}: {
  children: ReactNode;
  onClick?: () => void;
  href?: string;
  kind?: keyof typeof KIND;
  size?: keyof typeof SIZE;
  disabled?: boolean;
  busy?: boolean;
  type?: "button" | "submit";
  icon?: ReactNode;
  title?: string;
  full?: boolean;
}) {
  const className = [
    "inline-flex items-center justify-center rounded-lg border font-semibold",
    "transition-[background-color,color,box-shadow,transform] duration-150",
    "disabled:pointer-events-none disabled:opacity-45",
    KIND[kind],
    SIZE[size],
    full ? "w-full" : "",
  ].join(" ");

  const label = (
    <>
      {busy ? <Spinner /> : icon}
      <span className="truncate">{children}</span>
    </>
  );

  if (href && !disabled && !busy) {
    const external = href.startsWith("http");
    return external ? (
      <a className={className} href={href} title={title}>
        {label}
      </a>
    ) : (
      <Link className={className} href={href} title={title}>
        {label}
      </Link>
    );
  }
  return (
    <button
      type={type}
      className={className}
      onClick={onClick}
      disabled={disabled || busy}
      title={title}
      aria-busy={busy || undefined}
    >
      {label}
    </button>
  );
}

export function Spinner({ size = 14 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      className="animate-spin"
      aria-hidden="true"
    >
      <circle
        cx="12"
        cy="12"
        r="9"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.5"
        opacity="0.25"
      />
      <path
        d="M21 12a9 9 0 0 0-9-9"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function Field({
  label,
  value,
  onChange,
  rows,
  hint,
  mono,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  rows?: number;
  hint?: string;
  mono?: boolean;
  placeholder?: string;
}) {
  const shared = [
    "mt-1.5 w-full rounded-lg border border-line bg-ground px-3 py-2 text-sm",
    "transition-[border-color,box-shadow] placeholder:text-ink-faint",
    "focus:border-accent focus:outline-none focus:[box-shadow:var(--ring)]",
    mono ? "font-mono text-[13px]" : "",
    rows ? "font-serif text-[15px]/7" : "",
  ].join(" ");

  return (
    <label className="block">
      <span className="text-xs font-semibold text-ink-soft">{label}</span>
      {rows ? (
        <textarea
          className={shared}
          rows={rows}
          value={value}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <input
          className={shared}
          value={value}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
      {hint && <span className="mt-1.5 block text-xs text-ink-faint">{hint}</span>}
    </label>
  );
}

/* ── status ────────────────────────────────────────────────────────────── */

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

/** States where something is still happening, so the dot should move. */
const LIVE = new Set(["tailoring", "parsing", "sending", "created"]);

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

const PILL = {
  good: "border-good-line bg-good-bg text-good",
  warn: "border-warn-line bg-warn-bg text-warn",
  bad: "border-bad-line bg-bad-bg text-bad",
  plain: "border-line bg-sunken text-ink-soft",
} as const;

export function Status({ value }: { value: string }) {
  const tone = TONE[value] ?? "plain";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-semibold whitespace-nowrap ${PILL[tone]}`}
    >
      <span
        className={`relative inline-block size-1.5 rounded-full bg-current ${
          LIVE.has(value) ? "pulse" : ""
        }`}
      />
      {WORDS[value] ?? value}
    </span>
  );
}

/** A count or a code with a tone. Distinct from Status: this is a quantity or an
 *  identifier, and a quantity of zero should simply not be rendered. */
export function Tag({
  tone = "plain",
  children,
  icon,
  mono,
}: {
  tone?: keyof typeof PILL;
  children: ReactNode;
  icon?: ReactNode;
  mono?: boolean;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-xs font-medium whitespace-nowrap ${
        mono ? "font-mono" : ""
      } ${PILL[tone]}`}
    >
      {icon}
      {children}
    </span>
  );
}

/** A yes/no fact, in the two colours it can be. */
export function Verdict({ ok, children }: { ok: boolean; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 text-sm ${ok ? "text-good" : "text-bad"}`}
    >
      {ok ? <Check size={15} /> : <Cross size={15} />}
      {children}
    </span>
  );
}

/* ── errors and waiting ────────────────────────────────────────────────── */

/**
 * An error, shown as the thing to do about it.
 *
 * The API distinguishes "sign in", "connect Gmail", "this instance is not
 * configured" and "the run moved under you" with different status codes, and each
 * one has a different next step. Collapsing them into one red box would throw
 * away the part that helps.
 */
export function Problem({
  error,
  onRetry,
}: {
  error: ApiError;
  onRetry?: () => void;
}) {
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

  const headline =
    error.reason === "setup"
      ? "This instance is not set up for that yet"
      : error.reason === "offline"
        ? "The API is not answering"
        : "That did not work";

  return (
    <div className="rise flex gap-3 rounded-xl border border-bad-line bg-bad-bg p-4 shadow-card">
      <Alert size={18} className="mt-0.5 shrink-0 text-bad" />
      <div className="min-w-0">
        <p className="text-sm font-bold text-bad">{headline}</p>
        <p className="mt-1 font-serif text-[16px]/7 text-ink">{error.message}</p>
        {action && <div className="mt-3">{action}</div>}
      </div>
    </div>
  );
}

/**
 * Waiting, in the shape of what is coming.
 *
 * `what` is still taken and still used — as a label for assistive technology,
 * which gets a sentence while everyone else gets the outline of the list that is
 * about to appear. A spinner tells you to wait; this tells you what for.
 */
export function Loading({ what, rows = 3 }: { what: string; rows?: number }) {
  return (
    <div role="status" aria-label={`Loading ${what}`} className="space-y-2.5">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex items-center gap-3">
          <div className="skeleton h-9 w-9 shrink-0 rounded-lg" />
          <div className="min-w-0 flex-1 space-y-1.5">
            <div
              className="skeleton h-3 rounded"
              style={{ width: `${68 - i * 11}%` }}
            />
            <div
              className="skeleton h-2.5 rounded"
              style={{ width: `${44 - i * 7}%` }}
            />
          </div>
        </div>
      ))}
    </div>
  );
}
