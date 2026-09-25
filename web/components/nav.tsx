/**
 * The bar, and the only place that says who is signed in.
 *
 * It also carries the Gmail state, because that is the one piece of account
 * information with a consequence: connected means the send button will send,
 * not connected means it will ask first. Hiding that until the last screen is
 * how a person gets surprised at the one step that cannot be undone.
 */
"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { ApiError, api, connectGmailUrl, signInUrl } from "@/lib/api";
import type { Me } from "@/lib/types";

export function Nav() {
  const [me, setMe] = useState<Me | null>(null);
  const [checked, setChecked] = useState(false);

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
      className="border-b"
      style={{ borderColor: "var(--line)", background: "var(--surface)" }}
    >
      <div className="mx-auto flex w-full max-w-4xl flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
        <Link href="/" className="font-semibold tracking-tight">
          JobTailor
        </Link>
        <span className="text-xs" style={{ color: "var(--ink-faint)" }}>
          resume → posting → draft → your approval
        </span>

        <div className="ms-auto flex items-center gap-3 text-xs">
          {!checked ? null : me ? (
            <>
              <span style={{ color: "var(--ink-soft)" }}>{me.email}</span>
              {me.gmail_connected ? (
                <span style={{ color: "var(--good)" }}>Gmail connected</span>
              ) : (
                <a
                  href={connectGmailUrl}
                  className="underline"
                  style={{ color: "var(--accent)" }}
                >
                  Connect Gmail
                </a>
              )}
              <button
                className="underline"
                style={{ color: "var(--ink-faint)" }}
                onClick={async () => {
                  await api.post("/api/auth/logout");
                  location.assign("/");
                }}
              >
                Sign out
              </button>
            </>
          ) : (
            <a
              href={signInUrl}
              className="rounded-md px-3 py-1.5 font-medium"
              style={{ background: "var(--accent)", color: "var(--accent-ink)" }}
            >
              Sign in with Google
            </a>
          )}
        </div>
      </div>
    </header>
  );
}
