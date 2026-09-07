"use client";

import { useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useAuth } from "@/lib/auth-context";
import type { TailoredOutputOut } from "@/lib/types";
import { ProfilePanel } from "@/components/dashboard/ProfilePanel";
import { ResumePanel } from "@/components/dashboard/ResumePanel";
import { JobPanel } from "@/components/dashboard/JobPanel";
import { ResultPanel } from "@/components/dashboard/ResultPanel";
import { HistoryPanel } from "@/components/dashboard/HistoryPanel";

export default function DashboardPage() {
  const { user, loading, logout } = useAuth();
  const [resumeId, setResumeId] = useState<string | null>(null);
  const [activeOutput, setActiveOutput] = useState<TailoredOutputOut | null>(null);
  const resultRef = useRef<HTMLDivElement | null>(null);

  const outputIdForHistory = useMemo(() => activeOutput?.id ?? null, [activeOutput]);

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-slate-500">
        Loading…
      </div>
    );
  }

  if (!user) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-4 px-6 text-center">
        <p className="text-slate-600">You need to log in to use the dashboard.</p>
        <Link
          href="/login"
          className="rounded-lg bg-slate-900 px-5 py-2.5 text-sm font-semibold text-white hover:bg-slate-700"
        >
          Log in
        </Link>
      </div>
    );
  }

  function handleSubmit(output: TailoredOutputOut) {
    setActiveOutput(output);
    setTimeout(() => resultRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 100);
  }

  function handleHistorySelect(output: TailoredOutputOut) {
    setActiveOutput(output);
    setTimeout(() => resultRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 100);
  }

  return (
    <main className="min-h-screen bg-slate-50">
      <nav className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
          <div className="flex items-center gap-6">
            <Link href="/" className="text-lg font-semibold text-slate-900">
              JobTailor
            </Link>
            <span className="hidden text-sm text-slate-500 sm:inline">
              {user.full_name || user.email}
            </span>
          </div>
          <button
            onClick={logout}
            className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            Log out
          </button>
        </div>
      </nav>

      <div className="mx-auto grid max-w-5xl gap-6 px-6 py-8 lg:grid-cols-[1fr_320px]">
        <div className="space-y-6">
          <ProfilePanel />
          <ResumePanel selectedId={resumeId} onSelect={setResumeId} />
          <JobPanel resumeId={resumeId} onSubmitted={handleSubmit} />
          <div ref={resultRef}>
            {activeOutput ? (
              <ResultPanel
                key={activeOutput.id}
                output={activeOutput}
                onStatusChange={setActiveOutput}
              />
            ) : null}
          </div>
        </div>

        <aside>
          <HistoryPanel activeId={outputIdForHistory} onSelect={handleHistorySelect} />
        </aside>
      </div>
    </main>
  );
}
