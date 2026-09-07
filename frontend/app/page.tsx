import Link from "next/link";

export default function HomePage() {
  return (
    <main className="min-h-screen bg-gradient-to-b from-slate-50 to-white">
      <nav className="mx-auto flex max-w-5xl items-center justify-between px-6 py-5">
        <span className="text-xl font-semibold text-slate-900">JobTailor</span>
        <div className="flex items-center gap-3">
          <Link
            href="/login"
            className="rounded-lg px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100"
          >
            Log in
          </Link>
          <Link
            href="/login?mode=register"
            className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
          >
            Get started
          </Link>
        </div>
      </nav>

      <section className="mx-auto max-w-5xl px-6 pt-24 pb-16 text-center">
        <h1 className="mx-auto max-w-3xl text-4xl font-bold tracking-tight text-slate-900 sm:text-6xl">
          An agentic system that tailors your resume to every job.
        </h1>
        <p className="mx-auto mt-6 max-w-2xl text-lg text-slate-600">
          Paste a job posting (or grab one from LinkedIn), and the pipeline
          rewrites your summary and reorders your experience and projects to
          match the role — then renders an ATS-safe PDF, drafts an outreach
          message, and only emails it after you approve.
        </p>
        <div className="mt-10 flex flex-wrap items-center justify-center gap-4">
          <Link
            href="/dashboard"
            className="rounded-lg bg-slate-900 px-6 py-3 text-sm font-semibold text-white hover:bg-slate-700"
          >
            Open the dashboard
          </Link>
          <Link
            href="/login?mode=register"
            className="rounded-lg border border-slate-300 px-6 py-3 text-sm font-semibold text-slate-700 hover:bg-slate-50"
          >
            Create an account
          </Link>
        </div>
      </section>

      <section className="mx-auto max-w-5xl px-6 pb-24">
        <div className="grid gap-6 sm:grid-cols-3">
          {[
            {
              title: "RAG tailoring",
              body: "Retrieves the most relevant parts of your resume for each job and rewrites the summary, skills, experience, and projects to fit.",
            },
            {
              title: "Human approval gate",
              body: "Nothing is emailed automatically. Review the tailored resume and outreach message, approve it, then send it from your own Gmail.",
            },
            {
              title: "ATS-safe output",
              body: "Downloads as a clean PDF, and sends from your connected Gmail via OAuth — no app password needed.",
            },
          ].map((c) => (
            <div key={c.title} className="rounded-2xl border border-slate-200 p-6">
              <h3 className="text-base font-semibold text-slate-900">{c.title}</h3>
              <p className="mt-2 text-sm text-slate-600">{c.body}</p>
            </div>
          ))}
        </div>
      </section>
    </main>
  );
}
