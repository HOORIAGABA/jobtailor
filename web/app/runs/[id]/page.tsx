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
 */
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";

import { ApiError, api, connectGmailUrl, emlUrl, fileUrl } from "@/lib/api";
import type {
  Capabilities,
  Preview,
  RunDetail,
  SendResult,
} from "@/lib/types";
import {
  Button,
  Card,
  Empty,
  Field,
  Loading,
  Muted,
  Problem,
  Status,
} from "@/components/ui";

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
        .get<{ recipient: string; subject: string; body: string; confirm_token: string }>(
          `/api/runs/${id}/approved`,
        )
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
  if (!run) return <Loading what="this application" />;

  const consoleOnly = caps?.mail_provider?.startsWith("console") ?? false;

  return (
    <div className="space-y-6">
      <Card
        title={
          <span className="flex flex-wrap items-baseline gap-2">
            {run.role || "Untitled role"}
            {run.company && (
              <span className="text-sm font-normal" style={{ color: "var(--ink-soft)" }}>
                {run.company}
              </span>
            )}
            <Status value={run.status} />
          </span>
        }
        aside={
          <>
            {run.llm_calls} model call{run.llm_calls === 1 ? "" : "s"}
            {run.tokens ? ` · ${run.tokens.toLocaleString()} tokens` : ""}
          </>
        }
      >
        <Progress run={run} />
      </Card>

      {error && <Problem error={error} onRetry={() => void load()} />}

      {run.status === "failed" && (
        <Card title="It stopped" tone="bad">
          <p className="font-mono text-xs whitespace-pre-wrap">
            {run.error || "no reason recorded"}
          </p>
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
              <ul className="space-y-3">
                {preview.changes.map((change, i) => (
                  <li key={change.op_id ?? i} className="text-sm">
                    <div className="text-xs" style={{ color: "var(--ink-faint)" }}>
                      {change.op_kind}
                      {change.label ? ` · ${change.label}` : ` · ${change.ref_id}`}
                    </div>
                    {change.before && (
                      <p
                        className="mt-1 rounded px-2 py-1 line-through"
                        style={{ background: "var(--bad-bg)", color: "var(--ink-soft)" }}
                      >
                        {change.before}
                      </p>
                    )}
                    {change.after && (
                      <p
                        className="mt-1 rounded px-2 py-1"
                        style={{ background: "var(--good-bg)" }}
                      >
                        {change.after}
                      </p>
                    )}
                    {change.rationale && (
                      <p className="mt-1 text-xs" style={{ color: "var(--ink-soft)" }}>
                        {change.rationale}
                      </p>
                    )}
                    {change.cites && change.cites.length > 0 && (
                      <p className="mt-0.5 font-mono text-xs"
                         style={{ color: "var(--ink-faint)" }}>
                        supported by {change.cites.join(", ")}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </Card>

          {preview.gaps.length > 0 && (
            <Card
              title="What the posting wants and your resume does not show"
              tone="warn"
            >
              <Muted>
                These were left alone on purpose. Nothing was invented to cover
                them — that is the one thing this tool will not do for you.
              </Muted>
              <ul className="mt-3 space-y-1 text-sm">
                {preview.gaps.map((gap, i) => (
                  <li key={i}>
                    <span className="font-medium">{gap.requirement}</span>
                    {gap.severity && (
                      <span style={{ color: "var(--ink-soft)" }}>
                        {" "}— {gap.severity.replace(/_/g, " ")}
                      </span>
                    )}
                    {gap.closest_evidence && gap.closest_evidence.length > 0 && (
                      <span className="font-mono text-xs"
                            style={{ color: "var(--ink-faint)" }}>
                        {" "}closest: {gap.closest_evidence.join(", ")}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </Card>
          )}

          {preview.questions.length > 0 && (
            <Card title="Worth answering before you apply">
              <ul className="space-y-3 text-sm">
                {preview.questions.map((q, i) => (
                  <li key={i}>
                    <p>{q.question}</p>
                    {q.context && (
                      <p className="mt-1 text-xs" style={{ color: "var(--ink-faint)" }}>
                        about: {q.context}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            </Card>
          )}

          {preview.rejections.length > 0 && (
            <Card title="Changes that were refused" tone="warn">
              <Muted>
                The model proposed these and a rule refused them. Shown because a
                refusal you cannot see is a refusal you cannot check.
              </Muted>
              <ul className="mt-3 space-y-1 text-sm">
                {preview.rejections.map((r, i) => (
                  <li key={i}>
                    <span className="font-mono text-xs">{r.code}</span>
                    {r.op_kind ? (
                      <span style={{ color: "var(--ink-faint)" }}>
                        {" "}on {r.op_kind}
                      </span>
                    ) : null}
                    {r.detail ? ` — ${r.detail}` : ""}
                  </li>
                ))}
              </ul>
            </Card>
          )}

          <Standing standing={preview.standing} />

          <Card
            title="The email"
            aside={preview.problems.length > 0 ? "read the notes below" : undefined}
          >
            <Muted>
              Edit anything. What you approve is what gets sent — the exact text,
              not whatever the draft said a minute ago.
            </Muted>
            <div className="mt-3 space-y-3">
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
              <ul
                className="mt-3 space-y-1 rounded-md p-3 text-sm"
                style={{ background: "var(--warn-bg)", color: "var(--warn)" }}
              >
                {preview.problems.map((p, i) => (
                  <li key={i}>{p}</li>
                ))}
              </ul>
            )}
          </Card>

          <div
            className="sticky bottom-0 -mx-4 border-t px-4 py-3 sm:mx-0 sm:rounded-lg sm:border"
            style={{ background: "var(--surface)", borderColor: "var(--line)" }}
          >
            <div className="flex flex-wrap items-center gap-3">
              <Button
                kind="danger"
                onClick={() => void decide("reject")}
                busy={busy === "reject"}
              >
                Reject
              </Button>
              <span className="text-xs" style={{ color: "var(--ink-faint)" }}>
                Rejecting blocks sending. You keep the files either way.
              </span>
              <span className="ms-auto">
                <Button
                  kind="primary"
                  onClick={() => void decide("approve")}
                  busy={busy === "approve"}
                  disabled={!to.trim()}
                >
                  Approve
                </Button>
              </span>
            </div>
          </div>
        </>
      )}

      {/* the send panel */}
      {run.status === "approved" && !sent && (
        <Card title="Ready to send" tone="good">
          <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[auto_1fr]">
            <dt style={{ color: "var(--ink-faint)" }}>To</dt>
            <dd>{to}</dd>
            <dt style={{ color: "var(--ink-faint)" }}>Subject</dt>
            <dd>{subject}</dd>
          </dl>
          <p className="mt-3 text-sm whitespace-pre-wrap">{body}</p>

          <div className="mt-4 flex flex-wrap items-center gap-3">
            <Button kind="primary" onClick={() => void send()} busy={busy === "send"}>
              {consoleOnly ? "Produce the email" : "Send it"}
            </Button>
            <Button href={emlUrl(run.id)}>Download the .eml</Button>
            {consoleOnly ? (
              <span className="text-xs" style={{ color: "var(--ink-faint)" }}>
                This instance is set to the console backend: it writes the real
                message and dispatches nothing.
              </span>
            ) : (
              <a
                href={connectGmailUrl}
                className="text-xs underline"
                style={{ color: "var(--accent)" }}
              >
                Connect a different Gmail account
              </a>
            )}
          </div>
        </Card>
      )}

      {sent && (
        <Card title={sent.dispatched ? "Sent" : "Produced, not sent"} tone="good">
          <Muted>
            {sent.dispatched
              ? `Delivered through ${sent.provider}. Provider reference ${sent.provider_message_id}.`
              : `The console backend produced the message and dispatched nothing. ` +
                `Set MAIL_PROVIDER=gmail_api to send for real.`}
          </Muted>
          <p className="mt-2 text-sm">
            Attached: {sent.attachments.join(", ") || "nothing"}
          </p>
          <div className="mt-3">
            <Button href={emlUrl(run.id)}>Download what went out</Button>
          </div>
        </Card>
      )}

      {run.status === "rejected" && (
        <Card title="Rejected" tone="warn">
          <Muted>
            Nothing will be sent. The tailored files are still here — rejecting
            meant &ldquo;not on my behalf&rdquo;, not &ldquo;destroy the
            work&rdquo;.
          </Muted>
        </Card>
      )}

      {run.can_download && run.artifacts.length > 0 && (
        <Card title="Files">
          <ul className="flex flex-wrap gap-2">
            {run.artifacts.map((a) => (
              <li key={a.stage}>
                <Button href={fileUrl(run.id, a.stage)}>
                  {a.filename} · {Math.max(1, Math.round(a.bytes / 1024))} KB
                </Button>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}

/* ── progress ──────────────────────────────────────────────────────── */

/** The stages, named as the person would describe them rather than as S1–S11. */
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

function Progress({ run }: { run: RunDetail }) {
  const done = run.checkpoints.map((c) => c.stage);
  if (RUNNING.has(run.status)) {
    return (
      <div>
        <p className="text-sm">
          {STAGE_WORDS[run.stage] ?? run.stage ?? "starting"}…
        </p>
        <p className="mt-1 text-xs" style={{ color: "var(--ink-faint)" }}>
          {done.length} stage{done.length === 1 ? "" : "s"} done. This takes a few
          minutes; you can close the tab and come back.
        </p>
      </div>
    );
  }
  return (
    <p className="text-xs" style={{ color: "var(--ink-faint)" }}>
      {done.length} stage{done.length === 1 ? "" : "s"} recorded
      {run.created_at ? ` · started ${run.created_at.slice(0, 16).replace("T", " ")}` : ""}
    </p>
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
  standing: { demonstrated?: string[]; declared_only?: string[]; not_found?: string[] };
}) {
  const groups: [string, string[], string][] = [
    ["Shown with evidence", standing.demonstrated ?? [], "var(--good)"],
    ["Claimed, not shown", standing.declared_only ?? [], "var(--warn)"],
    ["Not in your resume", standing.not_found ?? [], "var(--bad)"],
  ];
  if (groups.every(([, items]) => items.length === 0)) return null;

  return (
    <Card title="Where you stand">
      <div className="grid gap-4 sm:grid-cols-3">
        {groups.map(([label, items, colour]) => (
          <div key={label}>
            <h3 className="text-xs font-semibold" style={{ color: colour }}>
              {label} ({items.length})
            </h3>
            <ul className="mt-1 space-y-0.5 text-sm">
              {items.length === 0 ? (
                <li style={{ color: "var(--ink-faint)" }}>—</li>
              ) : (
                items.map((item, i) => <li key={i}>{item}</li>)
              )}
            </ul>
          </div>
        ))}
      </div>
    </Card>
  );
}
