/**
 * Steps 5, 6 and 7 — watch it work, decide, send.
 *
 * ★ This is the screen the product is about. Everything before it is a machine
 * reading documents; this is where a person says yes, and it is the reason the
 * rest is allowed to be imperfect.
 *
 * Three views of one run, chosen by status rather than by routing, because they
 * are the same object at three moments and a person who reloads mid-run should
 * land where the run actually is:
 *
 *   created / tailoring    progress, from the checkpoints the API records
 *   needs_review           ★ the gate: diff, gaps, the draft, approve or reject
 *   approved               the send panel
 *   sent / rejected / failed   what happened, and the files either way
 *
 * **Approve or reject. Nothing in between.** One decision over the whole plan.
 * Rejecting blocks sending and keeps the files — the likeliest real rejection is
 * a good resume with a clumsy covering letter, and a gate that destroyed the
 * resume to punish the email would make the honest answer the expensive one.
 *
 * On the visual design of this page specifically: the diff is the only thing on
 * it that must be read carefully, so the diff is the only thing that gets colour
 * fills. Everything else — gaps, refusals, standing — is a hairline and a label.
 * A screen where six panels all shout has no emphasis left for the one that
 * matters.
 */
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";

import { ApiError, api, connectGmailUrl, emlUrl, fileUrl } from "@/lib/api";
import type { Capabilities, Preview, RunDetail, SendResult } from "@/lib/types";
import {
  Button,
  Card,
  Empty,
  Field,
  Loading,
  Muted,
  Page,
  PageHeader,
  Problem,
  Spinner,
  Stamp,
  Stat,
  Status,
  Tag,
} from "@/components/ui";
import { Alert, Arrow, Check, Cross, Doc, Mail } from "@/components/icons";

/** How often a running run is re-read. A stage takes tens of seconds, so this is
 *  responsive without being a busy loop — and polling rather than the SSE stream
 *  because a poll survives the free host's idle spin-down reconnecting, which an
 *  open stream does not. */
const POLL_MS = 3000;

const RUNNING = new Set(["created", "tailoring", "sending"]);

export default function RunPage() {
  const { id } = useParams<{ id: string }>();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // The gate's editable fields. Seeded from the preview and then owned here, so
  // a poll cannot overwrite what the person is typing.
  const [to, setTo] = useState("");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState<"" | "approve" | "reject" | "send">("");
  const [sent, setSent] = useState<SendResult | null>(null);

  /**
   * ★ The editable fields are seeded exactly once, and this ref is what
   * guarantees it.
   *
   * The first version of this screen refetched the preview whenever `load`
   * changed identity, and `load` depended on the preview — a loop that also
   * re-seeded `body` and `token` from the stored draft while the person was
   * typing. The result was a token covering the *original* text and a body
   * holding the *edited* text, so approving returned 409 "this approval does not
   * match the message that was previewed".
   *
   * The API was right and the UI was wrong, which is the whole argument for the
   * token: without it that bug would have sent the unedited draft and nobody
   * would have found out until the recruiter replied to the wrong email.
   */
  const seeded = useRef(false);

  const load = useCallback(async () => {
    try {
      const fresh = await api.get<RunDetail>(`/api/runs/${id}`);
      setRun(fresh);
      setError(null);
      return fresh;
    } catch (e) {
      setError(e as ApiError);
      return null;
    }
  }, [id]);

  useEffect(() => {
    api.get<Capabilities>("/api/capabilities").then(setCaps).catch(() => {});
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Seed the gate, once.
  useEffect(() => {
    if (!run || seeded.current) return;

    if (run.status === "needs_review") {
      seeded.current = true;
      api
        .get<Preview>(`/api/runs/${id}/preview`)
        .then((p) => {
          setPreview(p);
          setTo(p.recipient);
          setSubject(p.subject);
          setBody(p.body);
          setToken(p.confirm_token);
        })
        .catch((e: ApiError) => {
          seeded.current = false;
          setError(e);
        });
    } else if (run.status === "approved") {
      // A reload after approving. The token is fetched again rather than kept in
      // the browser, so closing the tab does not strand the run in `approved`
      // with no way to send it.
      seeded.current = true;
      api
        .get<{
          recipient: string;
          subject: string;
          body: string;
          confirm_token: string;
        }>(`/api/runs/${id}/approved`)
        .then((ready) => {
          setTo(ready.recipient);
          setSubject(ready.subject);
          setBody(ready.body);
          setToken(ready.confirm_token);
        })
        .catch((e: ApiError) => {
          seeded.current = false;
          setError(e);
        });
    }
  }, [id, run]);

  // Poll only while something is actually happening.
  useEffect(() => {
    if (!run || !RUNNING.has(run.status)) return;
    timer.current = setTimeout(() => void load(), POLL_MS);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [load, run]);

  async function decide(decision: "approve" | "reject") {
    setBusy(decision);
    setError(null);
    try {
      await api.post(`/api/runs/${id}/decision`, {
        decision,
        recipient: to,
        subject,
        body,
        confirm_token: token,
      });
      // The fields stay as they are: they now hold exactly what was approved,
      // and the send panel shows that. `seeded` stays true so the reload path
      // does not overwrite them.
      setPreview(null);
      await load();
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy("");
    }
  }

  async function send() {
    setBusy("send");
    setError(null);
    try {
      const result = await api.post<SendResult>(`/api/runs/${id}/send`, {
        recipient: to,
        subject,
        body,
        confirm_token: token,
      });
      setSent(result);
      await load();
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy("");
    }
  }

  if (!run && error) return <Problem error={error} onRetry={() => void load()} />;
  if (!run)
    return (
      <Card title="This application">
        <Loading what="this application" rows={3} />
      </Card>
    );

  const consoleOnly = caps?.mail_provider?.startsWith("console") ?? false;

  return (
    <div className="space-y-6 pb-4">
      <PageHeader
        back={{ href: "/app", label: "Applications" }}
        eyebrow={run.company || undefined}
        title={run.role || "Untitled role"}
        actions={<Status value={run.status} />}
      />

      <Card>
        <Progress run={run} />
        {(run.llm_calls > 0 || run.changes > 0) && (
          <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
            {/* `changes`, not `accepted`: an accepted `flag_gap` or `ask_user`
                changes nothing in the document, and a card saying "6 changes
                applied" above a diff listing 2 is a card the reader stops
                trusting. */}
            <Stat label="changes applied" value={run.changes} />
            <Stat label="refused" value={run.rejected} />
            <Stat
              label={`model call${run.llm_calls === 1 ? "" : "s"}`}
              value={run.llm_calls}
            />
            <Stat label="tokens" value={run.tokens.toLocaleString()} />
          </div>
        )}
      </Card>

      {error && <Problem error={error} onRetry={() => void load()} />}

      {run.status === "failed" && (
        <Card tone="bad" title="It stopped">
          <pre className="overflow-x-auto rounded-lg border border-bad-line bg-bad-bg p-3 font-mono text-xs whitespace-pre-wrap">
            {run.error || "no reason recorded"}
          </pre>
          <div className="mt-3">
            <Button href="/runs/new">Try another posting</Button>
          </div>
        </Card>
      )}

      {/* ★ the gate */}
      {run.status === "needs_review" && preview && (
        <>
          <Card
            title="What would change"
            aside={`${preview.changes.length} change${
              preview.changes.length === 1 ? "" : "s"
            }`}
          >
            {preview.changes.length === 0 ? (
              <Empty>
                Nothing was changed. That is a real answer — the resume already
                said what this posting asks for.
              </Empty>
            ) : (
              <ul className="space-y-4">
                {preview.changes.map((change, i) => (
                  <li
                    key={change.op_id ?? i}
                    className="border-b border-line-soft pb-4 last:border-0 last:pb-0"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <Tag>{change.op_kind}</Tag>
                      <span className="font-mono text-xs text-ink-faint">
                        {change.label || change.ref_id}
                      </span>
                    </div>

                    {change.before && (
                      <p className="mt-2 flex gap-2 rounded-lg border border-bad-line bg-bad-bg px-2.5 py-1.5 text-sm/6">
                        <span
                          aria-hidden
                          className="shrink-0 font-mono text-bad select-none"
                        >
                          −
                        </span>
                        <span className="text-ink-soft line-through decoration-bad/40">
                          {change.before}
                        </span>
                      </p>
                    )}
                    {change.after && (
                      <p className="mt-1.5 flex gap-2 rounded-lg border border-good-line bg-good-bg px-2.5 py-1.5 text-sm/6">
                        <span
                          aria-hidden
                          className="shrink-0 font-mono text-good select-none"
                        >
                          +
                        </span>
                        <span>{change.after}</span>
                      </p>
                    )}
                    {change.rationale && (
                      <p className="mt-2 text-sm/6 text-ink-soft">
                        {change.rationale}
                      </p>
                    )}
                    {change.cites && change.cites.length > 0 && (
                      <p className="mt-1.5 flex flex-wrap items-center gap-1.5 text-xs text-ink-faint">
                        supported by
                        {change.cites.map((c) => (
                          <span
                            key={c}
                            className="rounded border border-line bg-sunken px-1.5 py-0.5 font-mono"
                          >
                            {c}
                          </span>
                        ))}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </Card>

          {preview.gaps.length > 0 && (
            <Card
              tone="warn"
              title="What the posting wants and your resume does not show"
              aside={`${preview.gaps.length} gap${preview.gaps.length === 1 ? "" : "s"}`}
            >
              <Muted>
                These were left alone on purpose. Nothing was invented to cover
                them — that is the one thing this tool will not do for you.
              </Muted>
              <ul className="mt-3 space-y-2.5">
                {preview.gaps.map((gap, i) => (
                  <li
                    key={i}
                    className="border-b border-line-soft pb-2.5 text-sm last:border-0 last:pb-0"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{gap.requirement}</span>
                      {gap.severity && (
                        <Tag tone="warn">{gap.severity.replace(/_/g, " ")}</Tag>
                      )}
                    </div>
                    {gap.closest_evidence && gap.closest_evidence.length > 0 && (
                      <p className="mt-1 font-mono text-xs text-ink-faint">
                        closest: {gap.closest_evidence.join(", ")}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            </Card>
          )}

          {preview.questions.length > 0 && (
            <Card
              title="Worth answering before you apply"
              aside={`${preview.questions.length}`}
            >
              <ul className="space-y-3">
                {preview.questions.map((q, i) => (
                  <li key={i} className="flex gap-2.5">
                    <span className="mt-0.5 shrink-0 font-mono text-xs text-ink-faint">
                      {String(i + 1).padStart(2, "0")}
                    </span>
                    <div className="min-w-0">
                      <p className="text-sm/6">{q.question}</p>
                      {q.context && (
                        <p className="mt-0.5 text-xs text-ink-faint">
                          about: {q.context}
                        </p>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </Card>
          )}

          {preview.rejections.length > 0 && (
            <Card
              tone="warn"
              title="Changes that were refused"
              aside={`${preview.rejections.length} refused`}
            >
              <Muted>
                The model proposed these and a rule refused them. Shown because a
                refusal you cannot see is a refusal you cannot check.
              </Muted>
              <ul className="mt-4 space-y-3">
                {preview.rejections.map((r, i) => (
                  <li
                    key={i}
                    className="relative rounded-lg border border-bad-line bg-bad-bg px-3.5 pt-3 pb-3.5"
                  >
                    {/* ★ The one loud element in the product. A refusal is what
                        this system exists to be able to do, so it gets a
                        proofreader's mark rather than a polite grey label. */}
                    <span className="absolute end-2.5 top-2.5">
                      <Stamp />
                    </span>
                    <span className="font-mono text-xs font-medium text-bad">
                      {r.code}
                    </span>
                    {r.op_kind && (
                      <span className="ms-2 text-xs text-ink-faint">
                        on {r.op_kind}
                      </span>
                    )}
                    {r.detail && (
                      <p className="mt-1.5 pe-24 font-serif text-[15px]/6 text-ink-soft">
                        {r.detail}
                      </p>
                    )}
                    {r.ask_user && (
                      <p className="mt-1.5 text-xs text-ink-faint">
                        It asked rather than guessed — see the questions above.
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            </Card>
          )}

          <Standing standing={preview.standing} />

          <Card
            title="The email"
            aside={
              preview.problems.length > 0 ? "read the notes below" : undefined
            }
          >
            <Muted>
              Edit anything. What you approve is what gets sent — the exact text,
              not whatever the draft said a minute ago.
            </Muted>
            <div className="mt-4 space-y-3">
              <Field
                label="To"
                value={to}
                onChange={setTo}
                hint={
                  preview.recipient_candidates.length > 1
                    ? `also found in the posting: ${preview.recipient_candidates
                        .filter((c) => c !== to)
                        .join(", ")}`
                    : preview.recipient
                      ? "found in the posting"
                      : "no address was in the posting — add one"
                }
              />
              <Field label="Subject" value={subject} onChange={setSubject} />
              <Field label="Body" value={body} onChange={setBody} rows={12} />
            </div>

            {preview.problems.length > 0 && (
              <ul className="mt-4 space-y-1.5 rounded-lg border border-warn-line bg-warn-bg p-3 text-sm">
                {preview.problems.map((p, i) => (
                  <li key={i} className="flex gap-2">
                    <Alert size={15} className="mt-0.5 shrink-0 text-warn" />
                    <span className="text-ink-soft">{p}</span>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          {/* The decision. Sticky, because the gate is long and the point of it
              is that the person has scrolled through the whole thing — not that
              they hunted for a button at the end. */}
          <div className="sticky bottom-0 -mx-4 border-t border-line bg-surface/90 px-4 py-3 backdrop-blur-md sm:mx-0 sm:rounded-xl sm:border sm:shadow-float">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
              <Button
                kind="danger"
                onClick={() => void decide("reject")}
                busy={busy === "reject"}
                icon={<Cross size={15} />}
              >
                Reject
              </Button>
              <span className="min-w-0 flex-1 text-xs text-ink-faint">
                Rejecting blocks sending. You keep the files either way.
              </span>
              <Button
                kind="primary"
                onClick={() => void decide("approve")}
                busy={busy === "approve"}
                disabled={!to.trim()}
                icon={<Check size={15} />}
                title={!to.trim() ? "Add a recipient first" : undefined}
              >
                Approve
              </Button>
            </div>
          </div>
        </>
      )}

      {/* the send panel */}
      {run.status === "approved" && !sent && (
        <Card
          tone="good"
          title="Ready to send"
          aside={consoleOnly ? "console backend" : "Gmail"}
          footer={
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
              <Button
                kind="primary"
                onClick={() => void send()}
                busy={busy === "send"}
                icon={<Mail size={15} />}
              >
                {consoleOnly ? "Produce the email" : "Send it"}
              </Button>
              <Button href={emlUrl(run.id)} size="sm">
                Download the .eml
              </Button>
              <span className="min-w-0 flex-1 text-xs text-ink-faint">
                {consoleOnly ? (
                  "This instance is set to the console backend: it writes the real message and dispatches nothing."
                ) : (
                  <a
                    href={connectGmailUrl}
                    className="text-accent underline decoration-accent-line underline-offset-2"
                  >
                    Connect a different Gmail account
                  </a>
                )}
              </span>
            </div>
          }
        >
          <dl className="grid gap-x-4 gap-y-1.5 text-sm sm:grid-cols-[5rem_1fr]">
            <dt className="text-ink-faint">To</dt>
            <dd className="font-medium break-all">{to}</dd>
            <dt className="text-ink-faint">Subject</dt>
            <dd className="font-medium">{subject}</dd>
          </dl>
          <div className="mt-3 rounded-lg border border-line bg-sunken p-3 text-sm/6 whitespace-pre-wrap">
            {body}
          </div>
        </Card>
      )}

      {sent && (
        <Card
          tone="good"
          title={sent.dispatched ? "Sent" : "Produced, not sent"}
          aside={sent.sent_at?.slice(0, 16).replace("T", " ")}
        >
          <div className="flex gap-3">
            <Check size={18} className="mt-0.5 shrink-0 text-good" />
            <div className="min-w-0">
              <Muted>
                {sent.dispatched
                  ? `Delivered through ${sent.provider}. Provider reference ${sent.provider_message_id}.`
                  : "The console backend produced the message and dispatched nothing. Set MAIL_PROVIDER=gmail_api to send for real."}
              </Muted>
              <p className="mt-2 text-sm text-ink-soft">
                Attached: {sent.attachments.join(", ") || "nothing"}
              </p>
              <div className="mt-3">
                <Button href={emlUrl(run.id)} size="sm">
                  Download what went out
                </Button>
              </div>
            </div>
          </div>
        </Card>
      )}

      {run.status === "rejected" && (
        <Card tone="warn" title="Rejected">
          <Muted>
            Nothing will be sent. The tailored files are still here — rejecting
            meant &ldquo;not on my behalf&rdquo;, not &ldquo;destroy the
            work&rdquo;.
          </Muted>
        </Card>
      )}

      {run.can_download && run.artifacts.length > 0 && (
        <Card title="Files" aside={`${run.artifacts.length}`}>
          <ul className="grid gap-2 sm:grid-cols-2">
            {run.artifacts.map((a) => (
              <li key={a.stage}>
                <a
                  href={fileUrl(run.id, a.stage)}
                  className="flex items-center gap-3 rounded-lg border border-line px-3 py-2.5 transition-colors hover:bg-sunken"
                >
                  <span className="grid size-8 shrink-0 place-items-center rounded-md bg-accent-soft text-accent">
                    <Doc size={15} />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium">
                      {a.filename}
                    </span>
                    <span className="text-xs text-ink-faint">
                      {Math.max(1, Math.round(a.bytes / 1024))} KB
                    </span>
                  </span>
                  <Arrow size={15} className="shrink-0 text-ink-faint" />
                </a>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}

/* ── progress ──────────────────────────────────────────────────────── */

/** The stages, named as the person would describe them rather than as S1–S11.
 *  The key order is the pipeline order, and `Progress` relies on that. */
const STAGE_WORDS: Record<string, string> = {
  extract: "reading the posting",
  job_brief: "working out what the role wants",
  evidence: "matching it against your resume",
  standing: "grading what you can show",
  plan: "planning the changes",
  write: "writing them",
  validate: "checking every claim",
  apply: "applying what passed",
  diff: "building the diff",
  outreach: "drafting the email",
  resume_docx: "rendering the .docx",
  resume_pdf: "rendering the .pdf",
  sent_eml: "producing the message",
};

const STAGE_ORDER = Object.keys(STAGE_WORDS);

/**
 * What it is doing, and what it has already done.
 *
 * A named stage rather than a percentage. A run has thirteen recorded stages of
 * genuinely uneven length, so a progress bar would have to lie about position to
 * look smooth — and "checking every claim" tells a waiting person something a
 * bar at 61% does not.
 */
function Progress({ run }: { run: RunDetail }) {
  const done = new Set(run.checkpoints.map((c) => c.stage));
  const live = RUNNING.has(run.status);

  if (!live) {
    return (
      <p className="text-xs text-ink-faint">
        {done.size} stage{done.size === 1 ? "" : "s"} recorded
        {run.created_at
          ? ` · started ${run.created_at.slice(0, 16).replace("T", " ")}`
          : ""}
      </p>
    );
  }

  return (
    <div>
      <div className="flex items-center gap-2.5">
        <Spinner size={16} />
        <p className="text-sm font-medium">
          {STAGE_WORDS[run.stage] ?? run.stage ?? "starting"}…
        </p>
      </div>

      {/* One segment per stage. Filled means the API recorded a checkpoint for
          it, which is a fact rather than an estimate. */}
      <div className="mt-3 flex gap-1" aria-hidden>
        {STAGE_ORDER.map((stage) => (
          <span
            key={stage}
            title={STAGE_WORDS[stage]}
            className={`h-1.5 flex-1 rounded-full ${
              done.has(stage)
                ? "bg-accent"
                : stage === run.stage
                  ? "bg-accent/40"
                  : "bg-line"
            }`}
          />
        ))}
      </div>

      <p className="mt-2 text-xs text-ink-faint">
        {done.size} of {STAGE_ORDER.length} stages done. This takes a few
        minutes; you can close the tab and come back.
      </p>
    </div>
  );
}

/* ── standing ──────────────────────────────────────────────────────── */

/**
 * S2's three grades, which are the honest answer to "am I a fit".
 *
 * Three buckets and no score. A number would be maximisable, and the moment it
 * is maximisable the tool starts optimising it instead of telling the truth.
 */
function Standing({
  standing,
}: {
  standing: {
    demonstrated?: string[];
    declared_only?: string[];
    not_found?: string[];
  };
}) {
  const groups = [
    {
      label: "Shown with evidence",
      items: standing.demonstrated ?? [],
      dot: "bg-good",
      text: "text-good",
    },
    {
      label: "Claimed, not shown",
      items: standing.declared_only ?? [],
      dot: "bg-warn",
      text: "text-warn",
    },
    {
      label: "Not in your resume",
      items: standing.not_found ?? [],
      dot: "bg-bad",
      text: "text-bad",
    },
  ];
  if (groups.every((g) => g.items.length === 0)) return null;

  return (
    <Card title="Where you stand" aside="three grades, no score">
      <div className="grid gap-5 sm:grid-cols-3">
        {groups.map((group) => (
          <div key={group.label}>
            <h3
              className={`flex items-center gap-2 text-xs font-semibold ${group.text}`}
            >
              <span className={`size-1.5 rounded-full ${group.dot}`} />
              {group.label}
              <span className="text-ink-faint">({group.items.length})</span>
            </h3>
            <ul className="mt-2 space-y-1 text-sm">
              {group.items.length === 0 ? (
                <li className="text-ink-faint">—</li>
              ) : (
                group.items.map((item, i) => (
                  <li key={i} className="text-ink-soft">
                    {item}
                  </li>
                ))
              )}
            </ul>
          </div>
        ))}
      </div>
    </Card>
  );
}
