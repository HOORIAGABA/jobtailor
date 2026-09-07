"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { TailoredOutputOut } from "@/lib/types";
import { StatusBadge } from "../StatusBadge";

export function HistoryPanel({
  activeId,
  onSelect,
}: {
  activeId: string | null;
  onSelect: (o: TailoredOutputOut) => void;
}) {
  const [outputs, setOutputs] = useState<TailoredOutputOut[]>([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setOutputs(await api.listOutputs());
    } catch {
      /* ignore */
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section className="rounded-2xl border border-slate-200 p-6">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-base font-semibold text-slate-900">History</h2>
        <button
          onClick={load}
          disabled={loading}
          className="text-xs font-medium text-slate-500 hover:text-slate-800"
        >
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {outputs.length === 0 ? (
        <p className="text-sm text-slate-500">No tailored outputs yet.</p>
      ) : (
        <ul className="divide-y divide-slate-100">
          {outputs.map((o) => (
            <li key={o.id}>
              <button
                onClick={() => onSelect(o)}
                className={`flex w-full items-center justify-between gap-3 rounded-lg px-2 py-3 text-left hover:bg-slate-50 ${
                  activeId === o.id ? "bg-slate-50" : ""
                }`}
              >
                <span className="truncate text-sm text-slate-700">
                  Output {o.id.slice(0, 8)} · {o.job_post_id.slice(0, 8)}
                </span>
                <StatusBadge status={o.status} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
