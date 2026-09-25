# Evals

Stored cases that assert the product's central promise: **it will not say
anything your resume does not support.**

```bash
python -m scripts.eval              # every case, offline, zero API calls
python -m scripts.eval -v           # with the observations
python -m scripts.eval --case fabricated_number
python -m scripts.eval --live       # against the configured model. Spends quota.
```

They also run as part of `pytest` (`tests/test_evals.py`), so a regression in
the guarantee fails the build rather than waiting for someone to remember.

## There is no score

Every other part of this project refuses to produce a maximisable number, and
this is where that refusal is most tempting to abandon — a single
"quality: 82%" is so much easier to put in a README. It is also the number that,
once it exists, gets tuned against; and tuning a prompt against a number
computed from the same model's output is how a system learns to look good
rather than be good.

A case therefore reports **observations** (what was refused, applied, flagged)
and is judged against **assertions it names itself**. Cases pass or fail. The
suite reports counts. Nothing averages.

## Writing a case

One JSON file in `cases/`:

```jsonc
{
  "name": "fabricated_number",
  "why":  "Why this case exists. Required, and checked — a case without a "
          "rationale is one nobody can judge when it starts failing in 2027.",
  "resume":  { /* a RawResume */ },
  "posting": "the job posting, as text",
  "model": {                    // the stored answers; omit to run live only
    "brief":    { /* JobBriefDraft */ },
    "plan":     { "ops": [ /* … */ ] },
    "written":  { "bullets": [ /* … */ ] },
    "outreach": { /* DraftOutreach */ }
  },
  "expect": {
    "refusals":           ["fabricated_number"],   // rules that must fire
    "accepts":            ["set_summary"],         // ops that must be applied
    "rejects_op":         ["rewrite_bullet"],      // ops that must NOT be
    "absent_from_resume": ["40%"],                 // text that must not appear
    "present_in_resume":  ["saved the team a day a week"],
    "gaps_include":       ["Kubernetes"],
    "render_clean":       true,                    // survives the round trip
    "max_bullets_lost":   0,
    "no_ats_problems":    true
  }
}
```

The stored answers replay through every stage's **real** parsing, validation and
retry logic — `ScriptedClient` removes the network and the non-determinism, not
the code path. What offline mode exercises is the deterministic half: the
validator, the application, the diff, the ATS pass, the renderer. That is where
the guarantees live. A regression there is a broken promise; a change in the
model's phrasing is a Tuesday.

## The cases

| case | what it pins down |
| --- | --- |
| `fabricated_number` | a metric that is nowhere in the resume is refused |
| `honest_rewrite` | a grounded rewrite is **accepted** — without this, a validator that refused everything would pass the suite |
| `keyword_stuffing` | terms appended with no new meaning are refused |
| `seniority_escalation` | "contributed to" must not become "led" |

## Two cases have already earned their place

**`keyword_stuffing` found a live hole.** Its first draft stuffed Kubernetes and
FastAPI and passed — but as `unsupported_entity`, because those names are not in
the resume at all. The stuffing rule was never reached. Rewritten to stuff with
*grounded* terms (Python, SQL, PostgreSQL, Docker — all genuinely in the
resume), the bullet was **accepted**: `is_keyword_stuffing` compared the two
sentences with Jaccard similarity, and appending lowers similarity, so the
longer the keyword list the more "changed" the sentence scored. The check
rewarded exactly what it existed to catch. It now checks appending structurally:
if every new word is a capability name or the grammar needed to bolt a list onto
a sentence, no meaning was added.

**`honest_rewrite` exists to stop the suite lying.** Three cases asserting that
bad edits are refused are all satisfied by a validator that refuses everything —
and the product would be broken while the tests stayed green.
