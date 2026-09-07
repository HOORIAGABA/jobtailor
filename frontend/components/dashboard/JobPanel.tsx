"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import type { TailoredOutputOut } from "@/lib/types";

export function JobPanel({
  resumeId,
  onSubmitted,
}: {
  resumeId: string | null;
  onSubmitted: (output: TailoredOutputOut) => void;
}) {
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  const [status, setStatus] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submit(source: "paste" | "url") {
    if (!resumeId) {
      setStatus("Upload a resume first.");
      return;
    }
    setSubmitting(true);
    setStatus(source === "url" ? "Fetching URL…" : "Submitting…");
    try {
      const payload =
        source === "url"
          ? { url: url || null, raw_text: null, resume_id: resumeId, source_type: "url" as const }
          : { raw_text: text || null, url: null, resume_id: resumeId, source_type: "paste" as const };
      const output = await api.submitJobPost(payload);
      setStatus("Submitted. Starting the tailoring pipeline…");
      onSubmitted(output);
    } catch (e) {
      setStatus(`Error: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="rounded-2xl border border-slate-200 p-6">
      <h2 className="mb-4 text-base font-semibold text-slate-900">Job posting</h2>

      <label className="block">
        <span className="mb-1 block text-xs font-medium text-slate-600">
          Paste the job posting text
        </span>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={6}
          placeholder="Paste the job posting text here…"
          className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
        />
      </label>

      <div className="my-3 flex items-center gap-3 text-xs text-slate-400">
        <div className="h-px flex-1 bg-slate-200" />
        or from a URL (non-LinkedIn links are auto-fetched)
        <div className="h-px flex-1 bg-slate-200" />
      </div>

      <label className="block">
        <span className="mb-1 block text-xs font-medium text-slate-600">Job URL</span>
        <input
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://example.com/careers/backend-engineer"
          className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
        />
      </label>

      <div className="mt-4 flex flex-wrap gap-3">
        <button
          onClick={() => submit("paste")}
          disabled={submitting || !text.trim()}
          className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-semibold text-white hover:bg-slate-700 disabled:opacity-40"
        >
          {submitting ? "Running…" : "Tailor resume (pasted text)"}
        </button>
        <button
          onClick={() => submit("url")}
          disabled={submitting || !url.trim()}
          className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-40"
        >
          Tailor from URL
        </button>
      </div>

      {status && <p className="mt-3 text-sm text-slate-600">{status}</p>}
    </section>
  );
}
