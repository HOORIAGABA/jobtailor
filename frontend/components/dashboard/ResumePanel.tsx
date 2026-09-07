"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { ResumeOut } from "@/lib/types";

export function ResumePanel({
  selectedId,
  onSelect,
}: {
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const [resumes, setResumes] = useState<ResumeOut[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [status, setStatus] = useState("");
  const [uploading, setUploading] = useState(false);

  async function load() {
    try {
      const list = await api.listResumes();
      setResumes(list);
      if (!selectedId && list.length > 0) onSelect(list[0].id);
    } catch {
      /* ignore */
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function upload() {
    if (!file) {
      setStatus("Choose a .pdf or .docx file first.");
      return;
    }
    setUploading(true);
    setStatus("Uploading & parsing…");
    try {
      const resume = await api.uploadResume(file);
      onSelect(resume.id);
      await load();
      setStatus(`Uploaded & parsed (version ${resume.version}).`);
      setFile(null);
    } catch (e) {
      setStatus(`Error: ${e instanceof Error ? e.message : e}`);
    } finally {
      setUploading(false);
    }
  }

  const selected = resumes.find((r) => r.id === selectedId);

  return (
    <section className="rounded-2xl border border-slate-200 p-6">
      <h2 className="mb-4 text-base font-semibold text-slate-900">Baseline resume</h2>

      <div className="flex flex-wrap items-center gap-3">
        <input
          type="file"
          accept=".pdf,.docx"
          onChange={(e) => setFile(e.target.files?.[0] || null)}
          className="text-sm text-slate-600 file:mr-3 file:rounded-lg file:border-0 file:bg-slate-100 file:px-3 file:py-2 file:text-sm file:font-medium file:text-slate-700 hover:file:bg-slate-200"
        />
        <button
          onClick={upload}
          disabled={uploading}
          className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-semibold text-white hover:bg-slate-700 disabled:opacity-50"
        >
          {uploading ? "Uploading…" : "Upload & parse"}
        </button>
      </div>

      {resumes.length > 0 && (
        <div className="mt-4">
          <span className="text-xs font-medium text-slate-600">Select resume:</span>
          <div className="mt-2 flex flex-wrap gap-2">
            {resumes.map((r) => (
              <button
                key={r.id}
                onClick={() => onSelect(r.id)}
                className={`rounded-full px-3 py-1 text-xs font-medium ${
                  r.id === selectedId
                    ? "bg-slate-900 text-white"
                    : "bg-slate-100 text-slate-700 hover:bg-slate-200"
                }`}
              >
                {r.label} · v{r.version}
              </button>
            ))}
          </div>
        </div>
      )}

      {status && <p className="mt-3 text-sm text-slate-600">{status}</p>}

      {selected && (
        <details className="mt-4 rounded-lg bg-slate-50 p-3 text-xs">
          <summary className="cursor-pointer font-medium text-slate-600">
            View parsed resume preview
          </summary>
          <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-slate-700">
            {JSON.stringify(selected.resume_json, null, 2)}
          </pre>
        </details>
      )}
    </section>
  );
}
