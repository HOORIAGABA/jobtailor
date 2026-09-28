/**
 * Steps 2 and 3 — check the parse, fix it, confirm it.
 *
 * ★ This screen exists because a parse is a hypothesis. Everything downstream —
 * every citation, every "this claim traces to line X" — is built on ids that are
 * frozen at confirm, so a mistake here is a mistake in every tailored resume
 * that follows, and it would be invisible at the point where it mattered.
 *
 * **The lines the parse could not place are shown first, verbatim.** They are the
 * only thing on this screen that is certainly wrong, and asking about a specific
 * piece of text is the only kind of confirmation prompt anyone completes. A
 * "looks good?" button over a rendered document gets pressed without reading.
 *
 * **Editing costs nothing.** `PUT /draft` does not call a model — it stores the
 * correction and re-derives. So the edit loop can be as long as the person wants
 * and the free tier does not care. `reparse` is the one that spends calls, which
 * is why it is a separate, less prominent button.
 */
"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";

import { ApiError, api } from "@/lib/api";
import type { RawResume, ResumeDetail } from "@/lib/types";
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
  Status,
} from "@/components/ui";
import { Arrow, Check } from "@/components/icons";

export default function ResumePage() {
  const { id } = useParams<{ id: string }>();
  const [resume, setResume] = useState<ResumeDetail | null>(null);
  const [draft, setDraft] = useState<RawResume | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState<"" | "save" | "confirm" | "reparse">("");
  const [saved, setSaved] = useState(false);

  const load = useCallback(() => {
    setError(null);
    api
      .get<ResumeDetail>(`/api/resumes/${id}`)
      .then((r) => {
        setResume(r);
        setDraft(r.raw);
      })
      .catch((e: ApiError) => setError(e));
  }, [id]);

  useEffect(load, [load]);

  async function save() {
    if (!resume || !draft) return;
    setBusy("save");
    setError(null);
    try {
      // `revision` goes back with the edit. Two tabs editing the same parse
      // would otherwise silently lose one set of corrections, and the person who
      // lost them would have no way to know.
      const updated = await api.put<ResumeDetail>(`/api/resumes/${id}/draft`, {
        raw: draft,
        revision: resume.revision,
      });
      setResume(updated);
      setDraft(updated.raw);
      setSaved(true);
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy("");
    }
  }

  async function confirm() {
    setBusy("confirm");
    setError(null);
    try {
      await api.post(`/api/resumes/${id}/confirm`);
      location.assign("/runs/new");
    } catch (e) {
      setError(e as ApiError);
      setBusy("");
    }
  }

  async function reparse() {
    setBusy("reparse");
    setError(null);
    try {
      const again = await api.post<ResumeDetail>(`/api/resumes/${id}/reparse`);
      setResume(again);
      setDraft(again.raw);
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy("");
    }
  }

  if (error && !resume) return <Problem error={error} onRetry={load} />;
  if (!resume || !draft)
    return (
      <Card title="The parse">
        <Loading what="the parse" rows={4} />
      </Card>
    );

  const locked = resume.status === "confirmed" || resume.status === "superseded";

  return (
    <div className="space-y-6 pb-4">
      <PageHeader
        back={{ href: "/app", label: "Applications" }}
        eyebrow={`v${resume.version} · revision ${resume.revision}`}
        title={resume.filename}
        lead="This is what was read out of your file. Everything downstream cites these lines, so it is worth two minutes now."
        actions={<Status value={resume.status} />}
      />

      {error && <Problem error={error} onRetry={load} />}

      {resume.unplaced_lines.length > 0 && (
        <Card
          tone="warn"
          title={`${resume.unplaced_lines.length} line${
            resume.unplaced_lines.length === 1 ? "" : "s"
          } did not make it`}
          aside="in your file, not in the parse"
        >
          <Muted>
            These appear in your file and not in the parse below. Add them to the
            right section, or ignore them if they were headers, page numbers or
            decoration.
          </Muted>
          <ul className="mt-3 space-y-1.5">
            {resume.unplaced_lines.map((line, i) => (
              <li
                key={i}
                className="rounded-md border border-line bg-sunken px-2.5 py-1.5 font-mono text-xs break-words"
              >
                {line}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {resume.invented_lines.length > 0 && (
        <Card tone="bad" title="Text that is not in your file">
          <Muted>
            The parse produced these and your document does not contain them.
            Delete or correct them — a claim that is not yours must not travel.
          </Muted>
          <ul className="mt-3 space-y-1.5">
            {resume.invented_lines.map((line, i) => (
              <li
                key={i}
                className="rounded-md border border-bad-line bg-bad-bg px-2.5 py-1.5 font-mono text-xs break-words"
              >
                {line}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {resume.structure_warnings.length > 0 && (
        <Card tone="warn" title="Structure worth a look">
          <ul className="space-y-1.5 text-sm/6">
            {resume.structure_warnings.map((w, i) => (
              <li key={i} className="flex gap-2.5">
                <span className="mt-2 size-1.5 shrink-0 rounded-full bg-warn" />
                <span className="text-ink-soft">{w}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card title="Contact">
        <div className="grid gap-3 sm:grid-cols-2">
          {(
            [
              ["full_name", "Name"],
              ["email", "Email"],
              ["phone", "Phone"],
              ["location", "Location"],
              ["linkedin", "LinkedIn"],
              ["github", "GitHub"],
              ["website", "Website"],
            ] as const
          ).map(([key, label]) => (
            <Field
              key={key}
              label={label}
              value={draft.contact[key] ?? ""}
              onChange={(v) =>
                setDraft({ ...draft, contact: { ...draft.contact, [key]: v } })
              }
            />
          ))}
        </div>
      </Card>

      <Card title="Summary">
        <Field
          label="As written in your file"
          value={draft.summary}
          onChange={(v) => setDraft({ ...draft, summary: v })}
          rows={3}
        />
      </Card>

      {draft.sections.length === 0 ? (
        <Card tone="bad" title="Sections">
          <Empty>
            No sections were found. That usually means the layout defeated the
            extractor — try &ldquo;Ask the model again&rdquo;, or paste the text
            into a .txt and upload that.
          </Empty>
        </Card>
      ) : (
        draft.sections.map((section, si) => (
          <Card
            key={si}
            title={
              <input
                className="w-full rounded-md border border-transparent bg-transparent px-1.5 py-1 -mx-1.5 text-sm font-semibold tracking-tight transition-colors hover:border-line hover:bg-ground focus:border-accent focus:bg-ground focus:outline-none"
                value={section.heading}
                aria-label="Section heading"
                onChange={(e) => {
                  const sections = [...draft.sections];
                  sections[si] = { ...section, heading: e.target.value };
                  setDraft({ ...draft, sections });
                }}
              />
            }
            aside={`${section.entries.length} entr${
              section.entries.length === 1 ? "y" : "ies"
            }`}
          >
            <div className="space-y-3">
              {section.entries.map((entry, ei) => (
                <div
                  key={ei}
                  className="rounded-lg border border-line-soft bg-sunken p-3"
                >
                  <div className="grid gap-3 sm:grid-cols-3">
                    <Field
                      label="Title"
                      value={entry.title}
                      onChange={(v) => {
                        const sections = structuredClone(draft.sections);
                        sections[si].entries[ei].title = v;
                        setDraft({ ...draft, sections });
                      }}
                    />
                    <Field
                      label="Organisation"
                      value={entry.org}
                      onChange={(v) => {
                        const sections = structuredClone(draft.sections);
                        sections[si].entries[ei].org = v;
                        setDraft({ ...draft, sections });
                      }}
                    />
                    <Field
                      label="Dates"
                      value={entry.dates}
                      onChange={(v) => {
                        const sections = structuredClone(draft.sections);
                        sections[si].entries[ei].dates = v;
                        setDraft({ ...draft, sections });
                      }}
                    />
                  </div>
                  <div className="mt-3">
                    <Field
                      label="Bullets — one per line"
                      value={entry.bullets.join("\n")}
                      rows={Math.max(2, entry.bullets.length)}
                      hint="Blank lines are dropped."
                      onChange={(v) => {
                        const sections = structuredClone(draft.sections);
                        sections[si].entries[ei].bullets = v
                          .split("\n")
                          .map((b) => b.trim())
                          .filter(Boolean);
                        setDraft({ ...draft, sections });
                      }}
                    />
                  </div>
                </div>
              ))}
            </div>
          </Card>
        ))
      )}

      {/* The actions follow you down the page. This screen is long by design —
          it is the whole parse — and a confirm button at the bottom of a long
          scroll is a confirm button people press without reaching. */}
      <div className="sticky bottom-0 -mx-4 border-t border-line bg-surface/90 px-4 py-3 backdrop-blur-md sm:mx-0 sm:rounded-xl sm:border sm:shadow-float">
        {locked ? (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <Check size={16} className="shrink-0 text-good" />
            <p className="min-w-0 flex-1 text-sm/6 text-ink-soft">
              Confirmed. The ids in this parse are frozen, which is what lets
              every later change cite a specific line.
            </p>
            <Button kind="primary" href="/runs/new" icon={<Arrow size={15} />}>
              Tailor for a posting
            </Button>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <Button onClick={save} busy={busy === "save"} size="sm">
              Save corrections
            </Button>
            <Button onClick={reparse} busy={busy === "reparse"} size="sm">
              Ask the model again
            </Button>
            <span className="min-w-0 flex-1 text-xs text-ink-faint">
              {saved && <span className="text-good">Saved. </span>}
              Saving costs nothing. Asking again spends model calls.
            </span>
            <Button
              kind="primary"
              onClick={confirm}
              busy={busy === "confirm"}
              icon={<Check size={15} />}
            >
              This is right — confirm
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
