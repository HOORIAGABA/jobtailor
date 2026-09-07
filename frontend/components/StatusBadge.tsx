"use client";

import type { OutputStatus } from "@/lib/types";

const styles: Record<OutputStatus, string> = {
  pending: "bg-amber-100 text-amber-800",
  processing: "bg-amber-100 text-amber-800",
  ready: "bg-emerald-100 text-emerald-800",
  approved: "bg-blue-100 text-blue-800",
  sent: "bg-slate-800 text-white",
  failed: "bg-red-100 text-red-800",
};

export function StatusBadge({ status }: { status: OutputStatus }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${styles[status]}`}
    >
      {status}
    </span>
  );
}
