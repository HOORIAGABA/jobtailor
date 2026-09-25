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
import { Button, Card, Empty, Loading, Muted, Problem } from "@/components/ui";

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
      const run = await api.post<{ id: string }>("/api/runs", {
        resume_id: resumeId,
        job,
      });
      location.assign(`/runs/${run.id}`);
    } catch (e) {
      setError(e as ApiError);
      setStarting(false);
    }
  }

  if (error && resumes === null) return <Problem error={error} onRetry={load} />;
  if (resumes === null) return <Loading what="your resumes" />;

  if (resumes.length === 0) {
    return (
      <Card title="Confirm a resume first">
        <Empty>
          A run cites specific lines of a confirmed parse, so there has to be one
          before there can be a run.
        </Empty>
        <div className="mt-4">
          <Button kind="primary" href="/">
            Back to your resumes
          </Button>
        </div>
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      {error && <Problem error={error} />}

      <Card title="Paste the posting">
        <Muted>
          The whole thing — requirements, responsibilities, the team blurb. What
          looks like padding is often where the recruiter&apos;s own words are,
          and those are what the email can honestly echo.
        </Muted>
        <div className="mt-3">
          <textarea
            className="w-full rounded-md border px-3 py-2 text-sm"
            style={{ background: "var(--ground)", borderColor: "var(--line)" }}
            rows={14}
            value={job}
            placeholder={
              "Senior Backend Engineer — Annova\n\n" +
              "We are looking for…\n\nRequirements\n• …"
            }
            onChange={(e) => setJob(e.target.value)}
          />
          <p className="mt-1 text-xs" style={{ color: "var(--ink-faint)" }}>
            {job.trim().length.toLocaleString()} characters
          </p>
        </div>
      </Card>

      <Card title="Which resume">
        <div className="space-y-2">
          {resumes.map((resume) => (
            <label key={resume.id} className="flex items-center gap-3 text-sm">
              <input
                type="radio"
                name="resume"
                value={resume.id}
                checked={resumeId === resume.id}
                onChange={() => setResumeId(resume.id)}
              />
              <span className="font-medium">{resume.filename}</span>
              <span className="text-xs" style={{ color: "var(--ink-faint)" }}>
                v{resume.version} · {resume.sections.length} sections ·{" "}
                {resume.skills} skills
              </span>
            </label>
          ))}
        </div>
      </Card>

      <div className="flex flex-wrap items-center gap-3">
        <Button
          kind="primary"
          onClick={start}
          busy={starting}
          disabled={!resumeId || job.trim().length < 80 || caps?.read_only === true}
        >
          Tailor it
        </Button>
        <span className="text-xs" style={{ color: "var(--ink-faint)" }}>
          {caps?.read_only
            ? caps.why
            : job.trim().length < 80
              ? "Paste the posting first."
              : "Takes a few minutes. Nothing is sent — it stops for your approval."}
        </span>
      </div>
    </div>
  );
}
