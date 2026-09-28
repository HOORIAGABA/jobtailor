/**
 * The dashboard — where the flow starts, and where it is resumed.
 *
 * One screen holds both lists because the product's unit of work spans them: a
 * run belongs to a confirmed résumé, and the commonest thing a returning person
 * wants is the run that is waiting for them. Two separate pages would mean
 * remembering which one had the thing you left half-finished.
 *
 * It lives at `/app` rather than `/` so that `/` can be the landing page for
 * everyone, signed in or not. The old arrangement showed the pitch only on a
 * signed-out API error, which meant it was invisible to anyone who had ever
 * signed in — and permanently invisible on a laptop with `DEV_USER_EMAIL` set,
 * where every request is authenticated.
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
  Page,
  PageHeader,
  Problem,
  Row,
  Status,
} from "@/components/ui";
import { Arrow, Shield, Upload } from "@/components/icons";

export default function Dashboard() {
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

  /* Signed out, this page is not a broken dashboard — it is an invitation, with
     a route back to the page that explains the product. */
  if (error?.reason === "signin") {
    return (
      <Page>
        <PageHeader
          back={{ href: "/", label: "What JobTailor does" }}
          title="Sign in to start"
          lead="Your résumés and applications live behind a sign-in. Signing in asks for your name and email only; permission to send mail is requested later, at the approval step."
        />
        <Card>
          <div className="flex flex-wrap items-center gap-3">
            <Button kind="primary" href={signInUrl}>
              Sign in with Google
            </Button>
            <Button href="/">See what it does first</Button>
          </div>
        </Card>
      </Page>
    );
  }

  const waiting = (runs ?? []).filter((r) => r.status === "needs_review");
  const confirmed = (resumes ?? []).filter((r) => r.status === "confirmed");

  return (
    <Page>
      <div className="space-y-6">
        <PageHeader
          eyebrow="Your work"
          title="Applications"
          lead="A run belongs to a confirmed résumé. Anything needing a decision from you is at the top."
          actions={
            confirmed.length > 0 && !caps?.read_only ? (
              <Button kind="primary" href="/runs/new" icon={<Arrow size={15} />}>
                Tailor for a posting
              </Button>
            ) : undefined
          }
        />

        {error && <Problem error={error} onRetry={load} />}

        {caps?.read_only && (
          <div className="flex gap-3 rounded-xl border border-accent-line bg-accent-soft p-4">
            <Shield size={18} className="mt-0.5 shrink-0 text-accent" />
            <div className="min-w-0">
              <p className="text-sm font-bold">
                This instance serves stored runs
              </p>
              <p className="mt-0.5 font-serif text-[15px]/6 text-ink-soft">
                {caps.why}
              </p>
            </div>
          </div>
        )}

        {waiting.length > 0 && (
          <Card
            tone="warn"
            title="Waiting for your decision"
            aside={`${waiting.length} run${waiting.length === 1 ? "" : "s"}`}
          >
            <div className="-mb-4">
              {waiting.map((run) => (
                <Row
                  key={run.id}
                  title={`${run.role || "Untitled role"}${
                    run.company ? ` · ${run.company}` : ""
                  }`}
                  meta={`${run.changes} change${
                    run.changes === 1 ? "" : "s"
                  } proposed · ${run.rejected} refused`}
                  action={
                    <Button kind="primary" size="sm" href={`/runs/${run.id}`}>
                      Review
                    </Button>
                  }
                />
              ))}
            </div>
          </Card>
        )}

        <Card
          title="Your résumé"
          aside={resumes?.length ? `${resumes.length} uploaded` : undefined}
          footer={
            <div className="flex flex-wrap items-center gap-3">
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
                kind={resumes?.length ? "plain" : "primary"}
                size="sm"
                busy={uploading}
                disabled={caps?.read_only}
                icon={<Upload size={14} />}
                onClick={() => picker.current?.click()}
              >
                {uploading ? "Uploading" : "Upload a résumé"}
              </Button>
              <span className="text-xs text-ink-faint">
                PDF, .docx or .txt — the one you would actually send.
              </span>
            </div>
          }
        >
          {resumes === null ? (
            <Loading what="resumes" rows={2} />
          ) : resumes.length === 0 ? (
            <Empty>
              Nothing uploaded yet. JobTailor reads the file, shows you what it
              understood, and asks you to confirm it before proposing a single
              change.
            </Empty>
          ) : (
            <div className="-mb-4">
              {resumes.map((resume) => (
                <Row
                  key={resume.id}
                  href={`/resumes/${resume.id}`}
                  title={resume.filename}
                  badge={<Status value={resume.status} />}
                  meta={`v${resume.version} · ${resume.sections.length} section${
                    resume.sections.length === 1 ? "" : "s"
                  } · ${resume.skills} skills${
                    resume.unplaced_lines.length
                      ? ` · ${resume.unplaced_lines.length} line${
                          resume.unplaced_lines.length === 1 ? "" : "s"
                        } to place`
                      : ""
                  }`}
                />
              ))}
            </div>
          )}
        </Card>

        <Card
          title="Applications"
          aside={
            confirmed.length === 0
              ? "confirm a résumé first"
              : `${runs?.length ?? 0} total`
          }
        >
          {runs === null ? (
            <Loading what="runs" />
          ) : runs.length === 0 ? (
            <Empty
              action={
                confirmed.length > 0 && !caps?.read_only ? (
                  <Button kind="primary" size="sm" href="/runs/new">
                    Tailor for a posting
                  </Button>
                ) : undefined
              }
            >
              No applications yet. Paste a job posting and JobTailor proposes the
              changes it can justify — and tells you which ones it refused.
            </Empty>
          ) : (
            <div className="-mb-4">
              {runs.map((run) => (
                <Row
                  key={run.id}
                  href={`/runs/${run.id}`}
                  title={run.role || "Untitled role"}
                  badge={<Status value={run.status} />}
                  meta={[
                    run.company,
                    run.changes ? `${run.changes} changes` : null,
                    run.rejected ? `${run.rejected} refused` : null,
                    run.llm_calls
                      ? `${run.llm_calls} model call${run.llm_calls === 1 ? "" : "s"}`
                      : null,
                    run.tokens ? `${run.tokens.toLocaleString()} tokens` : null,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                />
              ))}
            </div>
          )}
        </Card>

        {resumes !== null && resumes.length === 0 && (
          <Muted>
            New here? The landing page walks through what happens at each step and
            what the tool refuses to do — <a href="/">read that first</a>.
          </Muted>
        )}
      </div>
    </Page>
  );
}
