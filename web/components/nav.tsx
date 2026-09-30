/**
 * The bar, and the only place that says who is signed in.
 *
 * It also carries the Gmail state, because that is the one piece of account
 * information with a consequence: connected means the send button will send, not
 * connected means it will ask first. Hiding that until the last screen is how a
 * person gets surprised at the one step that cannot be undone. It is rendered as
 * a pill rather than a line of text for the same reason — at the moment someone is
 * about to press Send, glanceable beats readable.
 *
 * The bar is sticky and translucent. That is not fashion: the gate screen is
 * long, the decision at the bottom of it is irreversible, and "am I signed in as
 * the right person, is Gmail connected" is exactly the question you want
 * answerable without scrolling back up.
 */
"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { ApiError, api, connectGmailUrl, signInUrl } from "@/lib/api";
import type { Capabilities, Me } from "@/lib/types";
import { Logo, Mail, Moon, Sun } from "@/components/icons";

const LINKS = [
  { href: "/app", label: "Applications" },
  { href: "/runs/new", label: "New" },
];

export function Nav() {
  const [me, setMe] = useState<Me | null>(null);
  const [checked, setChecked] = useState(false);
  // On the public read-only instance there is no sign-in, no Gmail and no new
  // run: every one of those buttons led to an error page. The API says which
  // mode it is in, so the header asks rather than guessing from the hostname.
  const [readOnly, setReadOnly] = useState(false);
  const path = usePathname();

  useEffect(() => {
    api
      .get<Capabilities>("/api/capabilities")
      .then((caps) => setReadOnly(caps.read_only === true))
      .catch(() => {});
  }, []);

  useEffect(() => {
    api
      .get<Me>("/api/auth/me")
      .then(setMe)
      .catch((e: ApiError) => {
        // A 401 here is the normal signed-out state, not a failure worth
        // shouting about. Anything else is left to the page that needs it.
        if (e.reason !== "signin") console.warn(e.message);
      })
      .finally(() => setChecked(true));
  }, []);

  return (
    <header
      className="sticky z-40 border-b border-line bg-ground/85 backdrop-blur-md"
      style={{ top: "env(safe-area-inset-top, 0px)" }}
    >
      <div className="mx-auto flex h-14 w-full max-w-5xl items-center gap-3 px-4 sm:gap-5">
        <Link
          href="/"
          className="flex shrink-0 items-center gap-2 text-[17px] font-extrabold tracking-tight"
        >
          <span className="grid size-7 place-items-center rounded-lg bg-ink text-ground">
            <Logo size={16} />
          </span>
          JobTailor
        </Link>

        <nav className="hidden items-center gap-1 sm:flex">
          {LINKS.filter((l) => !(readOnly && l.href === "/runs/new")).map((link) => {
            const active = path === link.href || path.startsWith(`${link.href}/`);
            return (
              <Link
                key={link.href}
                href={link.href}
                aria-current={active ? "page" : undefined}
                className={`rounded-lg px-2.5 py-1.5 text-sm transition-colors ${
                  active
                    ? "bg-sunken font-semibold text-ink"
                    : "text-ink-soft hover:bg-sunken hover:text-ink"
                }`}
              >
                {link.label}
              </Link>
            );
          })}
        </nav>

        <div className="ms-auto flex items-center gap-2">
          <ThemeToggle />
          {readOnly ? (
            <span className="rounded-full border border-line px-2.5 py-1 text-xs font-semibold text-ink-soft">
              Public demo · read-only
            </span>
          ) : !checked ? (
            <div className="skeleton h-7 w-20 rounded-full" />
          ) : me ? (
            <>
              {me.gmail_connected ? (
                <span
                  title={`Gmail connected — ${me.gmail_scopes.join(", ")}`}
                  className="hidden items-center gap-1.5 rounded-full border border-good-line bg-good-bg px-2 py-1 text-xs font-semibold text-good sm:inline-flex"
                >
                  <Mail size={13} />
                  Gmail
                </span>
              ) : (
                <a
                  href={connectGmailUrl}
                  className="hidden items-center gap-1.5 rounded-full border border-line px-2 py-1 text-xs font-semibold text-ink-soft transition-colors hover:bg-sunken hover:text-ink sm:inline-flex"
                >
                  <Mail size={13} />
                  Connect Gmail
                </a>
              )}
              <Account me={me} />
            </>
          ) : (
            <a
              href={signInUrl}
              className="inline-flex h-8 items-center rounded-lg bg-ink px-3 text-xs font-semibold text-ground transition-colors hover:bg-accent"
            >
              Sign in
            </a>
          )}
        </div>
      </div>
    </header>
  );
}

/**
 * The account chip. Closed it is an initial; open it shows the whole address.
 *
 * The address matters more here than in most products: it decides which Gmail
 * account an email would leave from. So the menu states it in full rather than
 * truncating it to fit, and sign-out lives behind one deliberate click instead of
 * sitting next to it as a bare link.
 */
function Account({ me }: { me: Me }) {
  const [open, setOpen] = useState(false);
  const initial = (me.name || me.email || "?").trim().charAt(0).toUpperCase();

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="menu"
        title={me.email}
        className="grid size-8 place-items-center rounded-full border border-line bg-sunken text-xs font-bold transition-colors hover:bg-surface"
      >
        {initial}
      </button>

      {open && (
        <>
          {/* Click-away, covering the whole viewport so a stray click anywhere
              closes the menu rather than only a click on the button. */}
          <button
            aria-label="Close menu"
            className="fixed inset-0 z-40 cursor-default"
            onClick={() => setOpen(false)}
          />
          <div
            role="menu"
            className="rise absolute end-0 z-50 mt-2 w-60 overflow-hidden rounded-xl border border-line bg-raised shadow-float"
          >
            <div className="border-b border-line-soft px-3 py-2.5">
              {me.name && <p className="text-sm font-semibold">{me.name}</p>}
              <p className="truncate text-xs text-ink-faint" title={me.email}>
                {me.email}
              </p>
            </div>
            <div className="px-3 py-2 font-serif text-[13px]/5 text-ink-soft">
              {me.can_send
                ? "Gmail is connected. An approved draft can be sent."
                : "Gmail is not connected. Approving a draft will ask first."}
            </div>
            <button
              role="menuitem"
              className="w-full border-t border-line-soft px-3 py-2.5 text-start text-sm transition-colors hover:bg-sunken"
              onClick={async () => {
                await api.post("/api/auth/logout");
                location.assign("/");
              }}
            >
              Sign out
            </button>
          </div>
        </>
      )}
    </div>
  );
}

/**
 * Light, dark, or whatever the machine says.
 *
 * Three states rather than two, because "follow the system" is the correct
 * default and a two-way toggle silently destroys it the first time it is pressed.
 * The choice is kept in `localStorage`, read once on mount, and applied as
 * `data-theme` on `<html>` — which is what the guarded selectors in `globals.css`
 * are watching for. The blocking script in the layout applies it before first
 * paint so the choice does not flash.
 *
 * Reading storage is wrapped: it throws outright in a private window with site
 * data blocked, and a theme button is not worth taking the page down for.
 */
type Choice = "system" | "light" | "dark";

function ThemeToggle() {
  const [choice, setChoice] = useState<Choice>("system");

  useEffect(() => {
    try {
      const saved = localStorage.getItem("theme") as Choice | null;
      if (saved === "light" || saved === "dark") setChoice(saved);
    } catch {
      /* no storage, no preference, system default stands */
    }
  }, []);

  function pick(next: Choice) {
    setChoice(next);
    try {
      if (next === "system") localStorage.removeItem("theme");
      else localStorage.setItem("theme", next);
    } catch {
      /* the attribute below still applies for this page view */
    }
    if (next === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = next;
  }

  // One button, cycling system → light → dark. The title says where it goes,
  // because an icon alone cannot express a third state.
  const next: Choice =
    choice === "system" ? "light" : choice === "light" ? "dark" : "system";

  return (
    <button
      onClick={() => pick(next)}
      title={`Theme: ${choice}. Switch to ${next}.`}
      aria-label={`Theme: ${choice}. Switch to ${next}.`}
      className="grid size-8 place-items-center rounded-lg text-ink-soft transition-colors hover:bg-sunken hover:text-ink"
    >
      {choice === "dark" ? (
        <Moon size={15} />
      ) : choice === "light" ? (
        <Sun size={15} />
      ) : (
        <span className="relative">
          <Sun size={15} />
          <span className="absolute -end-0.5 -bottom-0.5 size-1.5 rounded-full bg-accent" />
        </span>
      )}
    </button>
  );
}
