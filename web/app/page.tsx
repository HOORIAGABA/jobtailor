/**
 * The landing page — what a stranger sees.
 *
 * This is a **static server component** and that is the point: no hooks, no
 * fetches, no client JavaScript. The hero is in the HTML the server returns, so
 * it renders before anything loads and it is what a link preview and a search
 * crawler get. The previous version put this behind a signed-out API error, which
 * meant the best part of the site never appeared for anyone who had signed in
 * once — and never at all on a laptop with `DEV_USER_EMAIL` set, where every
 * request is authenticated.
 *
 * So the routes are now: `/` explains the product to anyone, `/app` is the
 * dashboard. The header carries the sign-in state, and `/app` refuses on its own
 * if nobody is signed in.
 *
 * The claim in the headline is the product's actual guarantee and not a slogan:
 * the model returns typed edit operations, a deterministic validator refuses the
 * ones it cannot trace to a line in the uploaded résumé, and the refusals are
 * shown rather than hidden. Anything vaguer would be marketing copy on a project
 * whose whole point is not overstating things.
 */
import Link from "next/link";

import { signInUrl } from "@/lib/api";
import { Button, Card, Stamp, Tag } from "@/components/ui";
import { Check, Cross } from "@/components/icons";

export default function Landing() {
  return (
    <>
      {/* ── hero ─────────────────────────────────────────────────────── */}
      <section className="dotfield">
        <div className="mx-auto grid w-full max-w-5xl items-center gap-11 px-4 py-14 sm:py-20 lg:grid-cols-[1.02fr_0.98fr] lg:gap-14">
          <div>
            <p className="eyebrow">Résumé tailoring with a paper trail</p>
            <h1 className="mt-3.5 text-[2.3rem]/[1.03] font-extrabold text-balance sm:text-[3.4rem]/[1.02]">
              Every line it changes, it can{" "}
              <em className="font-serif font-normal tracking-tight text-accent italic">
                point to
              </em>
              .
            </h1>
            <p className="mt-5 max-w-xl font-serif text-[19px]/[1.6] text-ink-soft">
              JobTailor reads a job posting, rewrites your résumé for it, and
              drafts the recruiter email. Anything it cannot trace back to a line
              you actually wrote gets refused — and shown to you, with the rule
              that refused it.
            </p>
            <div className="mt-7 flex flex-wrap gap-3">
              <Button kind="primary" size="lg" href={signInUrl}>
                Sign in with Google
              </Button>
              <Button size="lg" href="/app">
                Your applications
              </Button>
            </div>
            <p className="mt-3.5 max-w-md font-serif text-sm/6 text-ink-faint">
              Signing in asks for your name and email. Permission to send mail is
              requested later, at the approval step, when there is something to
              send.
            </p>
          </div>

          <Specimen />
        </div>
      </section>

      {/* ── counters ─────────────────────────────────────────────────── */}
      <section
        aria-label="By the numbers"
        className="border-y border-line bg-sunken"
      >
        <div className="mx-auto grid w-full max-w-5xl grid-cols-2 px-4 sm:grid-cols-4">
          {[
            ["9", "edit operations the model may return", false],
            ["13", "rules that can refuse one", false],
            ["1,121", "tests, on Linux and Windows", false],
            ["0", "scores, ratings or match percentages", true],
          ].map(([n, label, highlight]) => (
            <div key={label as string} className="py-5">
              <div
                className={`text-3xl leading-none font-extrabold ${
                  highlight ? "text-accent" : ""
                }`}
              >
                {n}
              </div>
              <div className="mt-1.5 max-w-[15rem] font-serif text-sm/6 text-ink-soft">
                {label}
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* ── the mechanism ────────────────────────────────────────────── */}
      <section className="mx-auto w-full max-w-5xl px-4 py-14">
        <p className="eyebrow">The mechanism</p>
        <h2 className="mt-2 text-2xl font-bold text-balance sm:text-[2rem]">
          The model never returns a résumé.
        </h2>
        <p className="mt-2.5 max-w-3xl font-serif text-[17px]/7 text-ink-soft">
          It returns typed edit operations — an id, a rationale, citations. A
          deterministic validator checks each one against your original document
          before anything is applied. That single decision is what makes the
          guarantee checkable instead of hopeful.
        </p>

        <div className="mt-8 grid gap-4 sm:grid-cols-3">
          <Step
            n="01 / PARSE"
            title="You confirm what it read"
            body="Reading a PDF is guesswork, so the guess is shown to you first — including the exact lines it could not place. Ids freeze on confirm, and every later citation points at them."
          />
          <Step
            n="02 / VALIDATE"
            title="Facts must appear verbatim"
            body="Numbers, employers, titles and dates must exist in the source. Reworded framing must be entailed by the original and must not promote you. Named skills must be in the evidence."
          />
          <Step
            n="03 / APPROVE"
            title="Your approval is bound to the text"
            body="The draft is editable, and approving signs the exact words you read. Change one character afterwards and the approval stops being valid. Nothing sends without it."
          />
        </div>
      </section>

      {/* ── three grades ─────────────────────────────────────────────── */}
      <section className="mx-auto w-full max-w-5xl px-4 pb-14">
        <p className="eyebrow">Instead of a match score</p>
        <h2 className="mt-2 text-2xl font-bold text-balance sm:text-[2rem]">
          Three grades, and the specifics behind each.
        </h2>
        <p className="mt-2.5 max-w-3xl font-serif text-[17px]/7 text-ink-soft">
          A percentage would be a thing to optimise, and optimising it is exactly
          what keyword stuffing does. So the answer is a list you can act on.
        </p>

        <div className="mt-7 grid gap-4 sm:grid-cols-3">
          <Grade
            tone="good"
            label="Shown with evidence"
            items={["Python", "Airflow", "Feature pipelines", "Model monitoring"]}
          />
          <Grade
            tone="warn"
            label="Claimed, not shown"
            items={["Kubernetes", "Mentoring"]}
          />
          <Grade
            tone="bad"
            label="Not in your résumé"
            items={["Ray / distributed training"]}
          />
        </div>
      </section>

      {/* ── limits ───────────────────────────────────────────────────── */}
      <section className="mx-auto w-full max-w-5xl px-4 pb-16">
        <p className="eyebrow">Limits, on purpose</p>
        <h2 className="mt-2 text-2xl font-bold text-balance sm:text-[2rem]">
          What it will not do for you.
        </h2>

        <div className="mt-7">
          <Card>
            <ul className="grid gap-3 sm:grid-cols-2 sm:gap-x-10">
              {[
                "Add a number that is not in your résumé",
                "Promote you to a seniority you have not held",
                "Claim a skill it cannot find evidence for",
                "Pad a list to beat a keyword filter",
                "Send anything you have not read",
                "Score you out of ten",
              ].map((line) => (
                <li key={line} className="flex gap-3">
                  <span className="mt-0.5 grid size-5 shrink-0 place-items-center rounded-md border border-bad-line bg-bad-bg text-bad">
                    <Cross size={12} />
                  </span>
                  <span className="font-serif text-[16px]/7 text-ink-soft">
                    {line}
                  </span>
                </li>
              ))}
            </ul>
            <p className="mt-5 border-t border-line-soft pt-4 font-serif text-[15px]/7 text-ink-faint">
              The validator runs in a layer that is forbidden — by a contract the
              build checks on every push — from importing a model client, the
              network or the database. It cannot quietly become a second model
              call.
            </p>
          </Card>
        </div>

        <div className="mt-8 flex flex-wrap items-center gap-3">
          <Button kind="primary" href={signInUrl}>
            Sign in with Google
          </Button>
          <Link
            href="https://github.com/HOORIAGABA/jobtailor"
            className="text-sm text-accent underline decoration-accent-line underline-offset-4 transition-colors hover:text-ink"
          >
            Read the source
          </Link>
        </div>
      </section>
    </>
  );
}

/**
 * ★ The specimen — the hero's real argument.
 *
 * One accepted edit with its citations, and underneath it one refused edit with
 * the rule that refused it, stamped. It is the product's whole thesis in a
 * picture, and the numbers in it are the real eval case (`fabricated_number`):
 * the writer proposes "cutting processing time by 35%", that figure is nowhere in
 * the résumé, and S5 refuses it.
 *
 * Hard-coded, and clearly an example rather than the viewer's data — it is a
 * landing page, and fetching a real run to render it would mean an empty box for
 * anyone not signed in, which is everyone this section is for.
 */
function Specimen() {
  return (
    <div className="overflow-hidden rounded-xl border border-line bg-surface shadow-float">
      <header className="flex flex-wrap items-center gap-2 border-b border-line-soft bg-sunken px-4 py-2.5">
        <span className="text-[13px] font-bold">
          Senior ML Engineer · Northwind
        </span>
        <Tag>14 changes</Tag>
        <Tag tone="bad">3 refused</Tag>
      </header>

      <div className="grid gap-3.5 px-4 py-4">
        <div className="flex flex-wrap items-center gap-2">
          <Tag mono>rewrite_bullet</Tag>
          <span className="font-mono text-xs text-ink-faint">
            experience · b-07
          </span>
        </div>

        <p className="flex gap-2.5 rounded-lg border border-bad-line bg-bad-bg px-3 py-2 font-serif text-[15px]/6">
          <span aria-hidden className="shrink-0 font-mono font-bold text-bad">
            −
          </span>
          <s className="text-ink-soft decoration-bad/50">
            Worked on the data pipeline for the recommendations team.
          </s>
        </p>
        <p className="flex gap-2.5 rounded-lg border border-good-line bg-good-bg px-3 py-2 font-serif text-[15px]/6">
          <span aria-hidden className="shrink-0 font-mono font-bold text-good">
            +
          </span>
          <span>
            Built and maintained the batch feature pipeline behind the
            recommendations service, in Python and Airflow.
          </span>
        </p>
        <p className="font-serif text-[15px]/6 text-ink-soft">
          The posting names Airflow twice. Both tools appear in your skills
          section and in this role&rsquo;s own bullets, so the claim is yours
          already — only the wording moved.
        </p>
        <p className="flex flex-wrap items-center gap-1.5 text-xs text-ink-faint">
          supported by
          {["b-07", "skill:airflow", "skill:python"].map((c) => (
            <span
              key={c}
              className="rounded border border-accent-line bg-accent-soft px-1.5 py-0.5 font-mono text-accent"
            >
              {c}
            </span>
          ))}
        </p>
      </div>

      <div className="relative border-t border-dashed border-line bg-sunken px-4 pt-4 pb-4.5">
        <span className="absolute end-3 top-3">
          <Stamp />
        </span>
        <span className="font-mono text-xs font-medium text-bad">
          fabricated_number
        </span>
        <p className="mt-1.5 pe-24 font-serif text-[15px]/6 text-ink-soft">
          The model proposed a figure that appears nowhere in your résumé. The
          original line was kept.
        </p>
        <div className="mt-2.5 rounded-lg border border-bad-line bg-bad-bg px-3 py-2 font-serif text-[15px]/6 text-ink-soft">
          Rebuilt the ingestion job,{" "}
          <b className="font-bold text-bad">cutting processing time by 35%</b>.
        </div>
      </div>
    </div>
  );
}

function Step({
  n,
  title,
  body,
}: {
  n: string;
  title: string;
  body: string;
}) {
  return (
    <div className="rounded-xl border border-line bg-surface p-5 shadow-card">
      <div className="font-mono text-xs font-bold tracking-wide text-accent">
        {n}
      </div>
      <h3 className="mt-3 text-[17px] font-bold">{title}</h3>
      <p className="mt-2 font-serif text-[15px]/7 text-ink-soft">{body}</p>
    </div>
  );
}

const GRADE = {
  good: { head: "text-good bg-good-bg", dot: "bg-good" },
  warn: { head: "text-warn bg-warn-bg", dot: "bg-warn" },
  bad: { head: "text-bad bg-bad-bg", dot: "bg-bad" },
} as const;

function Grade({
  tone,
  label,
  items,
}: {
  tone: keyof typeof GRADE;
  label: string;
  items: string[];
}) {
  return (
    <div className="overflow-hidden rounded-xl border border-line bg-surface">
      <header
        className={`flex items-center gap-2 border-b border-line-soft px-3.5 py-2.5 text-[13px] font-bold ${GRADE[tone].head}`}
      >
        <span className={`size-1.5 shrink-0 rounded-full ${GRADE[tone].dot}`} />
        {label} ({items.length})
      </header>
      <ul className="space-y-1 px-3.5 py-3 font-serif text-[15px]/6 text-ink-soft">
        {items.map((item) => (
          <li key={item} className="flex gap-2">
            <Check size={14} className="mt-1.5 shrink-0 opacity-40" />
            {item}
          </li>
        ))}
      </ul>
    </div>
  );
}
