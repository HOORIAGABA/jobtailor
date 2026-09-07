"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { OutputStatus, TailoredOutputOut } from "@/lib/types";
import { StatusBadge } from "../StatusBadge";

const PROCESSING = new Set<OutputStatus>(["pending", "processing"]);

interface ResumeView {
  summary_heading?: string | null;
  summary?: string;
  skills?: string[];
  experience?: { heading?: string; title: string; company?: string; dates?: string; bullets: string[] }[];
  projects?: { heading?: string; title: string; company?: string; dates?: string; bullets: string[] }[];
  certifications?: { title: string; company?: string; bullets: string[] }[];
  education?: { title: string; company?: string; bullets: string[] }[];
  leadership?: { title: string; company?: string; bullets: string[] }[];
}

function parseResume(output: TailoredOutputOut): ResumeView {
  try {
    const parsed = JSON.parse(output.tailored_json);
    if (parsed && typeof parsed === "object") return parsed as ResumeView;
  } catch {
    /* ignore */
  }
  return {};
}

function SectionList({
  title,
  items,
}: {
  title: string;
  items?: { title: string; company?: string; bullets: string[] }[];
}) {
  if (!items || items.length === 0) return null;
  return (
    <div>
      <h4 className="mt-4 border-b border-slate-200 pb-1 text-sm font-semibold uppercase tracking-wide text-slate-500">
        {title}
      </h4>
      {items.map((it, i) => (
        <div key={`${title}-${i}`} className="mt-3">
          <div className="flex items-baseline justify-between gap-3">
            <span className="text-sm font-semibold text-slate-800">{it.title}</span>
            {it.company && <span className="text-xs text-slate-500">{it.company}</span>}
          </div>
          <ul className="mt-1 list-disc pl-5 text-sm text-slate-700">
            {(it.bullets || []).map((b, j) => (
              <li key={j}>{b}</li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

export function ResultPanel({
  output,
  onStatusChange,
}: {
  output: TailoredOutputOut;
  onStatusChange: (o: TailoredOutputOut) => void;
}) {
  const [current, setCurrent] = useState(output);
  const [action, setAction] = useState("");
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    setCurrent(output);
  }, [output]);

  useEffect(() => {
    if (!PROCESSING.has(current.status)) return;
    pollTimer.current = setInterval(async () => {
      try {
        const fresh = await api.getOutput(current.id);
        setCurrent(fresh);
        onStatusChange(fresh);
      } catch {
        /* ignore, keep polling */
      }
    }, 8000);
    return () => {
      if (pollTimer.current) clearInterval(pollTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current.status, current.id]);

  const resume = parseResume(current);
  const canDownload = !PROCESSING.has(current.status) && current.status !== "failed";

  async function doDownload() {
    setAction("Downloading…");
    try {
      await api.downloadPdf(current.id);
      setAction("Downloaded.");
    } catch (e) {
      setAction(`Download failed: ${e instanceof Error ? e.message : e}`);
    }
  }

  async function doApprove() {
    setAction("Approving…");
    try {
      await api.approveOutput(current.id);
      const fresh = { ...current, status: "approved" as OutputStatus };
      setCurrent(fresh);
      onStatusChange(fresh);
      setAction("Approved.");
    } catch (e) {
      setAction(`Approve failed: ${e instanceof Error ? e.message : e}`);

      onStatusChange(current);
    }
  }

  async function doSend() {
    if (current.status !== "approved") {
      setAction("Approve the output first before sending.");
      return;
    }
    setAction("Sending…");
    try {
      await api.sendOutput(current.id);
      const fresh = { ...current, status: "sent" as OutputStatus };
      setCurrent(fresh);
      onStatusChange(fresh);
      setAction("Email sent.");
    } catch (e) {
      setAction(`Send failed: ${e instanceof Error ? e.message : e}`);

      onStatusChange(current);
    }
  }

  return (
    <section className="rounded-2xl border border-slate-200 p-6">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-base font-semibold text-slate-900">Result</h2>
        <StatusBadge status={current.status} />
      </div>

      {current.status === "failed" && (
        <p className="mb-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">
          {current.error || "The pipeline failed. Try submitting the job post again."}
        </p>
      )}

      {PROCESSING.has(current.status) && (
        <div className="mb-4 flex items-center gap-3 rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-800">
          <span className="h-3 w-3 animate-spin rounded-full border-2 border-amber-800 border-t-transparent" />
          Tailoring… status is “{current.status}”. This can take a few minutes.
        </div>
      )}

      {current.status === "ready" && !canDownload && null}
      {canDownload && (
        <div className="mb-4 flex flex-wrap gap-3">
          <button
            onClick={doDownload}
            disabled={PROCESSING.has(current.status)}
            className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-semibold text-white hover:bg-slate-700"
          >
            Download tailored resume (PDF)
          </button>
          {current.status !== "approved" && current.status !== "sent" && (
            <button
              onClick={doApprove}
              className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold text-slate-700 hover:bg-slate-50"
            >
              Approve
            </button>
          )}
          {current.status !== "sent" && (
            <button
              onClick={doSend}
              disabled={current.status !== "approved"}
              className={`rounded-lg px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-40 ${
                current.status === "approved" ? "bg-blue-600 hover:bg-blue-700" : "bg-slate-300"
              }`}
            >
              Send email
            </button>
          )}
        </div>
      )}

      {action && <p className="mb-3 text-sm text-slate-600">{action}</p>}

      <div className="grid gap-6 md:grid-cols-2">
        <div>
          <h3 className="text-sm font-semibold text-slate-800">
            {resume.summary_heading || "Summary"}
          </h3>
          <p className="mt-1 text-sm text-slate-700">{resume.summary}</p>

          {resume.skills && resume.skills.length > 0 && (
            <div className="mt-3">
              <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Skills</h4>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {resume.skills.map((s, i) => (
                  <span key={i} className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-700">
                    {s}
                  </span>
                ))}
              </div>
            </div>
          )}

          <SectionList title="Experience" items={resume.experience} />
          <SectionList title="Projects" items={resume.projects} />
          <SectionList title="Certifications" items={resume.certifications} />
          <SectionList title="Education" items={resume.education} />
          <SectionList title="Leadership" items={resume.leadership} />
        </div>

        <div>
          <h3 className="text-sm font-semibold text-slate-800">Outreach message</h3>
          <pre className="mt-1 whitespace-pre-wrap rounded-lg bg-slate-50 p-3 text-sm text-slate-700">
            {current.draft_message || "No draft message yet."}
          </pre>
        </div>
      </div>
    </section>
  );
}
