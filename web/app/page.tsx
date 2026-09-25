/**
 * Step 1 — where the flow starts, and where it is resumed.
 *
 * One screen holds both lists because the product's unit of work spans them: a
 * run belongs to a confirmed resume, and the commonest thing a returning person
 * wants is the run that is waiting for them. Two separate pages would mean
 * remembering which one had the thing you left half-finished.
 */
"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api, signInUrl } from "@/lib/api";
import type { Capabilities, ResumeSummary, RunSummary } from "@/lib/types";
import {
  Button,
  Card,
  Empty,
  Loading,
  Muted,
  Problem,
  Status,
} from "@/components/ui";

export default function Home() {
  const [resumes, setResumes] = useState<ResumeSummary[] | null>(null);
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [uploading, setUploading] = useState(false);
  const picker = useRef<HTMLInputElement>(null);

  const load = useCallback(() => {
    setError(null);
    api.get<Capabilities>("/api/capabilities").then(setCaps).catch(() => {});
    Promise.all([
      api.get<ResumeSummary[]>("/api/resumes"),
      api.get<RunSummary[]>("/api/runs"),
    ])
      .then(([r, u]) => {
        setResumes(r);
        setRuns(u);
      })
      .catch((e: ApiError) => setError(e));
  }, []);

  useEffect(load, [load]);

  async function upload(file: File) {
    setUploading(true);
    setError(null);
    try {
      const created = await api.upload<ResumeSummary>("/api/resumes", file);
      // Straight to the confirm screen: the parse is a hypothesis and the next
      // thing that has to happen is a person checking it.
      location.assign(`/resumes/${created.id}`);
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setUploading(false);
    }
  }

  if (error?.reason === "signin") {
    return (
      <div className="space-y-6">
        <Card title="Sign in to start">
          <Muted>
            JobTailor reads your resume and a job posting, proposes changes you
            can see line by line, drafts the covering email, and then stops.
            Nothing is sent until you have read it and pressed approve.
          </Muted>
          <p className="mt-3 text-xs" style={{ color: "var(--ink-faint)" }}>
            Signing in asks for your name and email address only. Permission to
            send mail is asked for later, at the approval step, where there is
            something to send.
          </p>
          <div className="mt-4">
            <Button kind="primary" href={signInUrl}>
              Sign in with Google
            </Button>
          </div>
        </Card>
      </div>
    );
  }

  const waiting = (runs ?? []).filter((r) => r.status === "needs_review");
  const confirmed = (resumes ?? []).filter((r) => r.status === "confirmed");

  return (
    <div className="space-y-6">
      {error && <Problem error={error} onRetry={load} />}

      {waiting.length > 0 && (
        <Card title="Waiting for your decision" tone="warn">
          <ul className="space-y-2">
            {waiting.map((run) => (
              <li key={run.id} className="flex flex-wrap items-center gap-3">
                <span className="text-sm font-medium">
                  {run.role || "Untitled role"}
                  {run.company ? ` · ${run.company}` : ""}
                </span>
                <span className="text-xs" style={{ color: "var(--ink-faint)" }}>
                  {run.changes} change{run.changes === 1 ? "" : "s"}
                </span>
                <span className="ms-auto">
                  <Button kind="primary" href={`/runs/${run.id}`}>
                    Review
                  </Button>
                </span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card
        title="Your resume"
        aside={caps?.read_only ? "this instance is read-only" : undefined}
      >
        {resumes === null ? (
          <Loading what="resumes" />
        ) : resumes.length === 0 ? (
          <Empty>
            Nothing uploaded yet. A PDF or .docx — the one you would actually
            send.
          </Empty>
        ) : (
          <ul className="space-y-2">
            {resumes.map((resume) => (
              <li
                key={resume.id}
                className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b pb-2 last:border-0"
                style={{ borderColor: "var(--line)" }}
              >
                <span className="text-sm font-medium">{resume.filename}</span>
                <span className="text-xs" style={{ color: "var(--ink-faint)" }}>
                  v{resume.version} · {resume.sections.length} section
                  {resume.sections.length === 1 ? "" : "s"} · {resume.skills} skills
                </span>
                <Status value={resume.status} />
                <span className="ms-auto">
                  <Button href={`/resumes/${resume.id}`}>
                    {resume.status === "needs_confirm" ? "Check the parse" : "Open"}
                  </Button>
                </span>
              </li>
            ))}
          </ul>
        )}

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <input
            ref={picker}
            type="file"
            accept=".pdf,.docx,.txt"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void upload(file);
            }}
          />
          <Button
            kind="primary"
            busy={uploading}
            disabled={caps?.read_only}
            onClick={() => picker.current?.click()}
          >
            Upload a resume
          </Button>
          {caps?.read_only && (
            <span className="text-xs" style={{ color: "var(--ink-faint)" }}>
              {caps.why}
            </span>
          )}
        </div>
      </Card>

      <Card
        title="Applications"
        aside={
          confirmed.length === 0
            ? "confirm a resume first"
            : `${runs?.length ?? 0} total`
        }
      >
        {runs === null ? (
          <Loading what="runs" />
        ) : runs.length === 0 ? (
          <Empty>No applications yet.</Empty>
        ) : (
          <ul className="space-y-2">
            {runs.map((run) => (
              <li
                key={run.id}
                className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b pb-2 last:border-0"
                style={{ borderColor: "var(--line)" }}
              >
                <span className="text-sm font-medium">
                  {run.role || "Untitled role"}
                </span>
                {run.company && (
                  <span className="text-sm" style={{ color: "var(--ink-soft)" }}>
                    {run.company}
                  </span>
                )}
                <Status value={run.status} />
                <span className="text-xs" style={{ color: "var(--ink-faint)" }}>
                  {run.llm_calls} model call{run.llm_calls === 1 ? "" : "s"}
                  {run.tokens ? ` · ${run.tokens.toLocaleString()} tokens` : ""}
                </span>
                <span className="ms-auto">
                  <Button href={`/runs/${run.id}`}>Open</Button>
                </span>
              </li>
            ))}
          </ul>
        )}

        {confirmed.length > 0 && !caps?.read_only && (
          <div className="mt-4">
            <Button kind="primary" href="/runs/new">
              Tailor for a new posting
            </Button>
          </div>
        )}
      </Card>
    </div>
  );
}
