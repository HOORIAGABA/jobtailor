/**
 * Step 4 — the posting.
 *
 * A textarea, because that is how a job posting actually arrives: selected from
 * a page and pasted. A URL field would look tidier and would then have to fetch
 * and scrape a site that is usually behind Cloudflare, JavaScript rendering, or
 * both — and would fail in a way that looks like the product being broken.
 *
 * Starting a run answers 202 immediately. A run takes minutes, no HTTP request
 * survives that, and the previous version of this project lost a run to an idle
 * host killing the thread that was doing the work. So this navigates to the run
 * page and the progress arrives there.
 */
"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiError, api } from "@/lib/api";
import type { Capabilities, ResumeSummary } from "@/lib/types";
import {
  Button,
  Card,
  Empty,
  Loading,
  Muted,
  Page,
  PageHeader,
  Problem,
} from "@/components/ui";
import { Arrow, Doc } from "@/components/icons";

/** Below this, a posting has no requirements in it worth matching against. The
 *  API enforces its own bounds; this is only so the button explains itself. */
const ENOUGH = 80;

export default function NewRun() {
  const [resumes, setResumes] = useState<ResumeSummary[] | null>(null);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [resumeId, setResumeId] = useState("");
  const [job, setJob] = useState("");
  const [error, setError] = useState<ApiError | null>(null);
  const [starting, setStarting] = useState(false);

  const load = useCallback(() => {
    setError(null);
    api.get<Capabilities>("/api/capabilities").then(setCaps).catch(() => {});
    api
      .get<ResumeSummary[]>("/api/resumes")
      .then((all) => {
        const usable = all.filter((r) => r.status === "confirmed");
        setResumes(usable);
        if (usable.length > 0) setResumeId(usable[0].id);
      })
      .catch((e: ApiError) => setError(e));
  }, []);

  useEffect(load, [load]);

  async function start() {
    setStarting(true);
    setError(null);
    try {
      // `run_id`, not `id`. The API answers
      // `{"run_id": ..., "events": ...}` and this was typed `{id: string}`,
      // so `run.id` was `undefined` and a successful start navigated to
      // `/runs/undefined`. TypeScript cannot catch a hand-written type that is
      // confidently wrong — the same failure as the four bad field names in
      // `lib/types.ts`, which is why the shared types now come from the API
      // and one-off inline types like this are the remaining risk.
      const run = await api.post<{ run_id: string; events: string }>(
        "/api/runs",
        { resume_id: resumeId, job },
      );
      location.assign(`/runs/${run.run_id}`);
    } catch (e) {
      setError(e as ApiError);
      setStarting(false);
    }
  }

  if (error && resumes === null) return <Problem error={error} onRetry={load} />;
  if (resumes === null)
    return (
      <Card title="Your resumes">
        <Loading what="your resumes" rows={2} />
      </Card>
    );

  if (resumes.length === 0) {
    return (
      <>
        <PageHeader
          back={{ href: "/app", label: "Applications" }}
          title="Confirm a resume first"
        />
        <Card>
          <Empty
            action={
              <Button kind="primary" size="sm" href="/app">
                Back to your résumés
              </Button>
            }
          >
            A run cites specific lines of a confirmed parse, so there has to be
            one before there can be a run.
          </Empty>
        </Card>
      </>
    );
  }

  const short = job.trim().length < ENOUGH;
  const blocked = caps?.read_only === true;

  return (
    <div className="space-y-6">
      <PageHeader
        back={{ href: "/", label: "Overview" }}
        eyebrow="New application"
        title="Paste the posting"
        lead="The whole thing — requirements, responsibilities, the team blurb. What looks like padding is often where the recruiter's own words are, and those are what the email can honestly echo."
      />

      {error && <Problem error={error} />}

      <Card
        title="The posting"
        aside={`${job.trim().length.toLocaleString()} characters`}
      >
        <textarea
          className="w-full rounded-lg border border-line bg-ground px-3 py-2.5 text-sm/6 transition-[border-color,box-shadow] placeholder:text-ink-faint focus:border-accent focus:outline-none focus:[box-shadow:var(--ring)]"
          rows={14}
          value={job}
          placeholder={
            "Senior Backend Engineer — Annova\n\n" +
            "We are looking for…\n\nRequirements\n• …"
          }
          onChange={(e) => setJob(e.target.value)}
        />
      </Card>

      <Card title="Which resume" aside={`${resumes.length} confirmed`}>
        <div className="space-y-2">
          {resumes.map((resume) => {
            const picked = resumeId === resume.id;
            return (
              <label
                key={resume.id}
                className={`flex cursor-pointer items-center gap-3 rounded-lg border px-3 py-2.5 transition-colors ${
                  picked
                    ? "border-accent-line bg-accent-soft"
                    : "border-line hover:bg-sunken"
                }`}
              >
                <input
                  type="radio"
                  name="resume"
                  value={resume.id}
                  checked={picked}
                  onChange={() => setResumeId(resume.id)}
                  className="accent-accent"
                />
                <Doc
                  size={16}
                  className={picked ? "text-accent" : "text-ink-faint"}
                />
                <span className="min-w-0 flex-1 truncate text-sm font-medium">
                  {resume.filename}
                </span>
                <span className="shrink-0 text-xs text-ink-faint">
                  v{resume.version} · {resume.sections.length} sections ·{" "}
                  {resume.skills} skills
                </span>
              </label>
            );
          })}
        </div>
      </Card>

      <Card
        tone={blocked ? "plain" : "good"}
        footer={
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <Button
              kind="primary"
              onClick={start}
              busy={starting}
              icon={<Arrow size={15} />}
              disabled={!resumeId || short || blocked}
            >
              {starting ? "Starting" : "Tailor it"}
            </Button>
            <span className="text-xs text-ink-faint">
              {blocked
                ? caps.why
                : short
                  ? `Paste the posting first — ${ENOUGH - job.trim().length} more characters.`
                  : "Takes a few minutes. Nothing is sent; it stops for your approval."}
            </span>
          </div>
        }
      >
        <Muted>
          What happens next, in order: it reads the posting into a structured
          brief, matches each requirement against the evidence in your resume,
          proposes typed edits, refuses the ones it cannot trace to a line you
          wrote, drafts the email, renders the files — and stops.
        </Muted>
      </Card>
    </div>
  );
}
