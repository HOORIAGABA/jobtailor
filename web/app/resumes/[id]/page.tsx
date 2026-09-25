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
  Problem,
  Status,
} from "@/components/ui";

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
      const updated = await api.put<ResumeDetail>(
        `/api/resumes/${id}/draft`,
        { raw: draft, revision: resume.revision },
      );
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
  if (!resume || !draft) return <Loading what="the parse" />;

  const locked = resume.status === "confirmed" || resume.status === "superseded";

  return (
    <div className="space-y-6">
      {error && <Problem error={error} onRetry={load} />}

      <Card
        title={resume.filename}
        aside={
          <span className="flex items-center gap-2">
            v{resume.version} · revision {resume.revision}
            <Status value={resume.status} />
          </span>
        }
      >
        <Muted>
          This is what was read out of your file. Everything downstream cites
          these lines, so it is worth two minutes now.
        </Muted>
      </Card>

      {resume.unplaced_lines.length > 0 && (
        <Card
          title={`${resume.unplaced_lines.length} line${
            resume.unplaced_lines.length === 1 ? "" : "s"
          } did not make it`}
          tone="warn"
        >
          <Muted>
            These appear in your file and not in the parse below. Add them to the
            right section, or ignore them if they were headers, page numbers or
            decoration.
          </Muted>
          <ul className="mt-3 space-y-1">
            {resume.unplaced_lines.map((line, i) => (
              <li
                key={i}
                className="rounded border px-2 py-1 font-mono text-xs"
                style={{ borderColor: "var(--line)", background: "var(--ground)" }}
              >
                {line}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {resume.invented_lines.length > 0 && (
        <Card title="Text that is not in your file" tone="bad">
          <Muted>
            The parse produced these and your document does not contain them.
            Delete or correct them — a claim that is not yours must not travel.
          </Muted>
          <ul className="mt-3 space-y-1">
            {resume.invented_lines.map((line, i) => (
              <li key={i} className="font-mono text-xs">
                {line}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {resume.structure_warnings.length > 0 && (
        <Card title="Structure worth a look" tone="warn">
          <ul className="space-y-1 text-sm">
            {resume.structure_warnings.map((w, i) => (
              <li key={i}>{w}</li>
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
        <Card title="Sections" tone="bad">
          <Empty>
            No sections were found. That usually means the layout defeated the
            extractor — try Ask again, or paste the text into a .txt and upload
            that.
          </Empty>
        </Card>
      ) : (
        draft.sections.map((section, si) => (
          <Card
            key={si}
            title={
              <input
                className="w-full rounded border px-2 py-1 text-base font-semibold"
                style={{ background: "var(--ground)", borderColor: "var(--line)" }}
                value={section.heading}
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
            <div className="space-y-4">
              {section.entries.map((entry, ei) => (
                <div
                  key={ei}
                  className="rounded-md border p-3"
                  style={{ borderColor: "var(--line)" }}
                >
                  <div className="grid gap-2 sm:grid-cols-3">
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
                  <div className="mt-2">
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

      <div
        className="sticky bottom-0 -mx-4 border-t px-4 py-3 sm:mx-0 sm:rounded-lg sm:border"
        style={{ background: "var(--surface)", borderColor: "var(--line)" }}
      >
        {locked ? (
          <div className="flex flex-wrap items-center gap-3">
            <Muted>
              Confirmed. The ids in this parse are frozen, which is what lets
              every later change cite a specific line.
            </Muted>
            <span className="ms-auto">
              <Button kind="primary" href="/runs/new">
                Tailor for a posting
              </Button>
            </span>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-3">
            <Button onClick={save} busy={busy === "save"}>
              Save corrections
            </Button>
            <Button onClick={reparse} busy={busy === "reparse"}>
              Ask the model again
            </Button>
            <span className="text-xs" style={{ color: "var(--ink-faint)" }}>
              {saved ? "Saved. " : ""}Saving costs nothing. Asking again spends
              model calls.
            </span>
            <span className="ms-auto">
              <Button kind="primary" onClick={confirm} busy={busy === "confirm"}>
                This is right — confirm
              </Button>
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
