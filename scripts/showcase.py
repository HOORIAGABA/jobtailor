"""Five applications for the public demo: one candidate, five AI roles.

    python -m scripts.showcase            # check all five, then (re)seed them
    python -m scripts.showcase --check    # check only; writes nothing

Seeding always replaces what the demo account holds. The account is synthetic
and belongs to this script, so there is nothing in it to preserve — and a
second run that failed on a unique constraint, as the first version did,
is a worse default than a second run that just works.

**What is recorded and what is computed.** Each case stores the four answers a
model gave — the job brief, the plan, the rewritten lines, the email — and
replays them through the real `tailor()`: evidence matching, the validator,
apply, diff, the email checks and the PDF render all run for real, every time.
So a refusal on these screens is the guard refusing, not a picture of it. The
run page says so: `proof_json.seeded` is shown as a "replayed" note.

**Why five, and why these five.** Each one shows a different thing the product
claims, so a visitor can check every claim against a screen:

    meridian   fabricated_number     an invented accuracy figure is refused
    sadaf      seniority_escalation  "Fine-tuned" -> "Led the fine-tuning" is refused
    qalam      unsupported_entity    a vector database she never used is refused
    indus      keyword_stuffing      a tool list glued onto a bullet is refused
    rahbar     (nothing refused)     the honest case: the guard lets good edits through

**Nothing is seeded that fails its own check.** `verify()` asserts, per case,
that the expected refusal happened and nothing else was refused, that every
number in the tailored résumé is on the original, that every change carries a
source, that the email passed every message rule, and that the PDF rendered
clean. `tests/test_showcase.py` runs the same checks in CI, so a change to the
validator that breaks a demo screen fails the build instead of the demo.

The candidate is fictional — the same Zara Ahmed as `demo/Zara_Ahmed_CV.pdf`,
with `example.com` contact details — for the reason in `scripts/demo_seed.py`:
a public link must not publish a real person's CV.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.db import models
from app.db.session import session_scope
from app.domain.models import RawResume
from app.engine.normalize import normalize
from app.engine.validator import numbers
from app.pipeline.run import RunState, proof_checks, tailor
from scripts import demo_seed

logger = logging.getLogger("showcase")

# ── the candidate ─────────────────────────────────────────────────────
#
# Exactly the text of demo/Zara_Ahmed_CV.pdf, so a visitor who downloads the
# original and the tailored PDF is comparing like with like.

RESUME = RawResume.model_validate({
    "contact": {
        "full_name": "Zara Ahmed",
        "email": "zara.ahmed@example.com",
        "phone": "+92 300 0000000",
        "location": "Lahore, Pakistan",
        "linkedin": "linkedin.com/in/zara-ahmed-demo",
        "github": "github.com/zara-ahmed-demo",
        "website": "",
    },
    "summary": "Computer science graduate who builds LLM applications in "
               "Python. Hands-on experience with retrieval pipelines, prompt "
               "evaluation and FastAPI services, from an internship and a "
               "final-year NLP project.",
    "sections": [
        {"heading": "EXPERIENCE", "entries": [
            {"title": "Machine Learning Intern", "org": "Datum Analytics",
             "dates": "Jun 2025 - Nov 2025", "bullets": [
                 "Built a document question-answering prototype over 1,200 "
                 "internal PDFs using sentence-transformers embeddings and "
                 "FAISS retrieval",
                 "Wrote an evaluation script that compared three prompt "
                 "templates on 150 labelled questions and reported answer "
                 "accuracy for each template",
                 "Packaged the prototype as a FastAPI service in Docker so the "
                 "support team could test it",
                 "Helped clean and label training data for a support-ticket "
                 "classification model",
             ]},
            {"title": "Teaching Assistant",
             "org": "COMSATS University Islamabad",
             "dates": "Feb 2024 - Jan 2025", "bullets": [
                 "Ran weekly Python lab sessions for 40 students in "
                 "Programming Fundamentals and graded their assignments",
                 "Wrote automated test cases used to check lab submissions",
             ]},
        ]},
        {"heading": "PROJECTS", "entries": [
            {"title": "Urdu News Headline Classifier (final-year project)",
             "org": "", "dates": "2025", "bullets": [
                 "Fine-tuned a multilingual BERT model to classify Urdu news "
                 "headlines into 6 categories, reaching 87% accuracy on a "
                 "held-out test set",
                 "Built a Streamlit demo that shows the predicted category and "
                 "the model's confidence",
             ]},
            {"title": "Meeting Notes Summariser", "org": "", "dates": "2024",
             "bullets": [
                 "Python tool that transcribes meeting audio with Whisper and "
                 "summarises it with a local Llama model through Ollama",
             ]},
        ]},
        {"heading": "EDUCATION", "entries": [
            {"title": "BS Computer Science",
             "org": "COMSATS University Islamabad", "dates": "2021 - 2025",
             "bullets": ["CGPA 3.4 / 4.0. Relevant courses: Machine Learning, "
                         "Natural Language Processing, Databases"]},
        ]},
        {"heading": "SKILLS", "entries": [
            {"title": "", "org": "", "dates": "", "bullets": [
                "Languages: Python, SQL, JavaScript",
                "ML and NLP: PyTorch, scikit-learn, Hugging Face Transformers, "
                "sentence-transformers",
                "LLM tooling: OpenAI API, Ollama, LangChain, FAISS, prompt "
                "engineering",
                "Backend and data: FastAPI, PostgreSQL, Docker, Git, pandas, "
                "Streamlit",
            ]},
        ]},
    ],
})

# Line ids after `normalize` — a running counter across sections:
#   exp.1  intern      b.1 QA prototype  b.2 evaluation  b.3 FastAPI  b.4 labelling
#   exp.2  TA          b.1 labs          b.2 test cases
#   prj.3  classifier  b.1 BERT 87%      b.2 Streamlit
#   prj.4  summariser  b.1 Whisper/Ollama


def _grounded(posting: str, *statements: str) -> list[dict[str, Any]]:
    """Spans computed from the posting, never typed by hand.

    S1 drops any grounded element whose span does not contain its own
    statement. `demo_seed` learned that with hand-written offsets, which once
    produced a brief with zero requirements and a demo that showed nothing.
    """
    out = []
    for statement in statements:
        start = posting.index(statement)
        out.append({"statement": statement, "start": start,
                    "end": start + len(statement)})
    return out


def _term(term: str, kind: str, weight: int, required: bool = True,
          aliases: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"term": term, "kind": kind, "aliases": list(aliases),
            "required": required, "weight": weight}


@dataclass
class Case:
    key: str
    posting: str
    brief: dict[str, Any]
    plan: dict[str, Any]
    written: dict[str, Any]
    outreach: dict[str, Any]
    refused: tuple[str, ...]            # the codes that must be refused, exactly
    gaps: tuple[str, ...]               # terms that must stand as "not found"
    what_it_shows: str = ""
    answers: dict[str, Any] = field(init=False)

    def __post_init__(self) -> None:
        self.answers = {"brief": self.brief, "plan": self.plan,
                        "written": self.written, "outreach": self.outreach}


# ══ 1 · Meridian Labs — an invented number ═══════════════════════════

MERIDIAN = """Junior AI Engineer — Meridian Labs
Islamabad (hybrid) · Full-time

Meridian Labs builds document intelligence tools for banks and insurers in Pakistan and the Gulf. Our team of twelve ships LLM features that read contracts, claims and statements, and we care as much about being right as about being fast.

What you will do
- Build retrieval-augmented generation (RAG) features over customer documents
- Write and version prompts, and measure them with automated evaluations
- Serve models behind FastAPI services running in Docker
- Work with our platform team to deploy services on Kubernetes
- Help us move our agent workflows to LangGraph

What we are looking for
- Strong Python
- Experience building LLM applications: retrieval, prompt engineering, evaluation
- Vector search with FAISS or pgvector
- FastAPI and Docker
- 1-2 years of experience in an ML or backend role

Nice to have
- Kubernetes
- LangGraph or another agent framework
- Fine-tuning with LoRA

To apply, send your CV to careers@meridianlabs.example with the role in the subject line.
"""

CASE_MERIDIAN = Case(
    key="meridian",
    what_it_shows="An invented accuracy figure is refused; the honest "
                  "rewrite beside it is applied.",
    posting=MERIDIAN,
    brief={
        "company": "Meridian Labs", "role": "Junior AI Engineer",
        "seniority": "junior", "tone": "pragmatic",
        "role_narrative": (
            "Build retrieval-augmented generation features over bank and "
            "insurance documents. Write, version and evaluate prompts. Serve "
            "models behind FastAPI services in Docker, and help the platform "
            "team run them on Kubernetes."),
        "problems_to_solve": [
            {**g, "priority": "core"} for g in _grounded(
                MERIDIAN,
                "Build retrieval-augmented generation (RAG) features over "
                "customer documents",
                "Write and version prompts, and measure them with automated "
                "evaluations")],
        "success_signals": _grounded(
            MERIDIAN, "we care as much about being right as about being fast"),
        "hard_requirements": _grounded(
            MERIDIAN, "Strong Python",
            "Experience building LLM applications: retrieval, prompt "
            "engineering, evaluation",
            "Vector search with FAISS or pgvector", "FastAPI and Docker",
            "1-2 years of experience in an ML or backend role"),
        "terms": [
            _term("Python", "skill", 10),
            _term("FAISS", "tool", 8),
            _term("FastAPI", "tool", 8),
            _term("Docker", "tool", 7),
            _term("prompt engineering", "skill", 7),
            _term("Kubernetes", "tool", 5, required=False, aliases=("k8s",)),
            _term("LangGraph", "tool", 5, required=False),
            _term("LoRA", "skill", 4, required=False),
        ],
    },
    plan={"ops": [
        {"op": "set_summary",
         # "AI engineer" was the first draft of this line, and the summary
         # check refused it: the résumé never says "AI". The posting does.
         "text": "Computer science graduate who builds retrieval and prompt "
                 "evaluation for LLM applications in Python, and serves them "
                 "with FastAPI and Docker.",
         "rationale": "The posting opens with RAG, prompt evaluation and "
                      "FastAPI in Docker; the internship shows all three and "
                      "the old summary named none of them first.",
         "cites": ["exp.1.b.1", "exp.1.b.2", "exp.1.b.3"]},
        {"op": "rewrite_bullet", "bullet_id": "exp.1.b.1", "text": "",
         "target_terms": ["FAISS"],
         "rationale": "Name the pattern the posting asks for — retrieval over "
                      "documents — in the posting's own words."},
        # ★ refused: the writer adds a number the résumé does not have.
        {"op": "rewrite_bullet", "bullet_id": "exp.1.b.2", "text": "",
         "target_terms": ["prompt engineering"],
         "rationale": "Lead with the result of the prompt evaluation."},
        {"op": "flag_gap", "requirement": "Kubernetes", "severity": "minor",
         "closest_evidence": ["exp.1.b.3"]},
        {"op": "flag_gap", "requirement": "LangGraph", "severity": "minor",
         "closest_evidence": []},
        {"op": "flag_gap", "requirement": "Fine-tuning with LoRA",
         "severity": "minor", "closest_evidence": ["prj.3.b.1"]},
        {"op": "ask_user", "bullet_id": "exp.1.b.2",
         "question": "What accuracy did the best prompt template reach on the "
                     "150 questions? The real figure would be the strongest "
                     "line for this role — the tool will not guess it."},
    ]},
    written={"bullets": [
        {"bullet_id": "exp.1.b.1",
         "text": "Built a retrieval-based question-answering prototype over "
                 "1,200 internal PDFs using sentence-transformers embeddings "
                 "and FAISS"},
        {"bullet_id": "exp.1.b.2",
         "text": "Improved answer accuracy by 30% by comparing three prompt "
                 "templates on 150 labelled questions"},
    ]},
    outreach={
        "subject": "Application: Junior AI Engineer — Zara Ahmed, retrieval "
                   "and prompt evaluation",
        "body": (
            "I am applying for the Junior AI Engineer role at Meridian Labs. "
            "Retrieval over documents and measuring prompts are the two things "
            "your posting leads with, and they are the two things I did most "
            "in my internship.\n\n"
            "At Datum Analytics I built a question-answering prototype over "
            "1,200 internal PDFs using sentence-transformers embeddings and "
            "FAISS, and packaged it as a FastAPI service in Docker. I also "
            "wrote the evaluation script that compared three prompt templates "
            "on 150 labelled questions.\n\n"
            "I have not yet deployed services on Kubernetes or used LangGraph; "
            "both are listed as nice to have, and I would welcome the chance "
            "to learn them on your platform.\n\n"
            "Thank you for your time. My résumé is attached as a PDF, and I "
            "would be glad to arrange a short conversation about the role."),
        "cites": ["exp.1.b.1", "exp.1.b.2", "exp.1.b.3"],
    },
    refused=("fabricated_number",),
    gaps=("Kubernetes", "LangGraph", "LoRA"),
)

# ══ 2 · Sadaf Health — a promotion ═══════════════════════════════════

SADAF = """Machine Learning Engineer, NLP — Sadaf Health
Karachi · Full-time

Sadaf Health turns clinical notes written in English and Urdu into structured data that hospitals can search. We are hiring an engineer to train and evaluate the language models behind it.

Responsibilities
- Fine-tune transformer models for classification and extraction on Urdu and English text
- Build evaluation sets and report model accuracy honestly, including where it fails
- Turn models into small demos clinicians can try
- Track experiments with MLflow

Requirements
- Python and PyTorch
- Hands-on experience with Hugging Face Transformers
- Experience with Urdu or other low-resource languages
- Experiment tracking with MLflow or Weights & Biases
- 2+ years of industry experience in NLP

Apply at jobs@sadafhealth.example.
"""

CASE_SADAF = Case(
    key="sadaf",
    what_it_shows='"Fine-tuned" becoming "Led the fine-tuning" is refused as a '
                  "seniority claim the résumé does not make.",
    posting=SADAF,
    brief={
        "company": "Sadaf Health", "role": "Machine Learning Engineer, NLP",
        "seniority": "mid", "tone": "pragmatic",
        "role_narrative": (
            "Train and evaluate transformer models that turn Urdu and English "
            "clinical notes into structured data. Build evaluation sets, "
            "report accuracy honestly, and turn models into demos clinicians "
            "can try."),
        "problems_to_solve": [
            {**g, "priority": "core"} for g in _grounded(
                SADAF,
                "Fine-tune transformer models for classification and "
                "extraction on Urdu and English text",
                "Build evaluation sets and report model accuracy honestly")],
        "success_signals": _grounded(
            SADAF, "Turn models into small demos clinicians can try"),
        "hard_requirements": _grounded(
            SADAF, "Python and PyTorch",
            "Hands-on experience with Hugging Face Transformers",
            "Experience with Urdu or other low-resource languages",
            "Experiment tracking with MLflow or Weights & Biases",
            "2+ years of industry experience in NLP"),
        "terms": [
            _term("Python", "skill", 9),
            _term("PyTorch", "tool", 8),
            _term("Hugging Face Transformers", "tool", 8,
                  aliases=("transformers",)),
            _term("BERT", "tool", 6, required=False),
            _term("Urdu", "domain", 8),
            _term("Streamlit", "tool", 4, required=False),
            _term("MLflow", "tool", 6),
        ],
    },
    plan={"ops": [
        {"op": "set_summary",
         "text": "Computer science graduate who fine-tunes and evaluates "
                 "transformer models for NLP, including a multilingual BERT "
                 "classifier for Urdu news headlines.",
         "rationale": "The Urdu classifier is the closest thing on the résumé "
                      "to this job, and the old summary led with LLM "
                      "applications instead.",
         "cites": ["prj.3.b.1", "exp.1.b.2"]},
        {"op": "rewrite_bullet", "bullet_id": "exp.1.b.2", "text": "",
         "target_terms": [],
         "rationale": "The posting asks for accuracy reported honestly; say "
                      "that this script measured accuracy per template."},
        # ★ refused: an ownership verb the original line does not have.
        {"op": "rewrite_bullet", "bullet_id": "prj.3.b.1", "text": "",
         "target_terms": ["BERT", "Urdu"],
         "rationale": "Put the fine-tuning first; it is the posting's first "
                      "responsibility."},
        {"op": "rewrite_bullet", "bullet_id": "prj.3.b.2", "text": "",
         "target_terms": ["Streamlit"],
         "rationale": "The posting wants demos clinicians can try; say that "
                      "this was one."},
        {"op": "flag_gap", "requirement": "MLflow", "severity": "major",
         "closest_evidence": ["exp.1.b.2"]},
        {"op": "flag_gap", "requirement": "2+ years of industry experience "
                                          "in NLP",
         "severity": "major", "closest_evidence": []},
    ]},
    written={"bullets": [
        {"bullet_id": "exp.1.b.2",
         "text": "Wrote an evaluation script that measured answer accuracy for "
                 "each of three prompt templates on 150 labelled questions"},
        {"bullet_id": "prj.3.b.1",
         "text": "Led the fine-tuning of a multilingual BERT model that "
                 "classifies Urdu news headlines into 6 categories, reaching "
                 "87% accuracy on a held-out test set"},
        {"bullet_id": "prj.3.b.2",
         "text": "Built a Streamlit demo so that non-technical users could try "
                 "the classifier and see the model's confidence"},
    ]},
    outreach={
        "subject": "Application: Machine Learning Engineer, NLP — Zara Ahmed, "
                   "Urdu text classification",
        "body": (
            "I am applying for the Machine Learning Engineer, NLP role at "
            "Sadaf Health. My final-year project was a fine-tuned multilingual "
            "BERT model for Urdu text, which is close to the problem your "
            "posting describes.\n\n"
            "The model classifies Urdu news headlines into 6 categories and "
            "reached 87% accuracy on a held-out test set, and I built a "
            "Streamlit demo that shows each prediction with the model's "
            "confidence. During my internship I also wrote an evaluation "
            "script that compared prompt templates on labelled questions.\n\n"
            "I have not yet used MLflow, and my industry experience is one "
            "internship rather than two years; I would rather say so plainly.\n\n"
            "Thank you for your time. My résumé is attached as a PDF, and I "
            "would welcome a short conversation about the role."),
        "cites": ["prj.3.b.1", "prj.3.b.2", "exp.1.b.2"],
    },
    refused=("seniority_escalation",),
    gaps=("MLflow",),
)

# ══ 3 · Qalam AI — a tool she never used ═════════════════════════════

QALAM = """LLM Application Developer — Qalam AI
Remote (Pakistan) · Full-time

Qalam AI builds assistants that answer questions over company knowledge bases for customer support teams.

The work
- Build retrieval pipelines over support documents and help-centre articles
- Choose and tune embedding models
- Run our vector store (Pinecone) and keep the index fresh
- Build small internal tools with Python and Ollama for local testing
- Write evaluation sets so every change to a prompt is measured

You bring
- Python
- Experience with embeddings and vector search (Pinecone, FAISS or similar)
- Experience with LlamaIndex or LangChain
- Some experience running open models locally
- Clear written communication

Send your application to hiring@qalam.example.
"""

CASE_QALAM = Case(
    key="qalam",
    what_it_shows="A rewrite that swaps FAISS for Pinecone — the posting's "
                  "vector store, never used by the candidate — is refused.",
    posting=QALAM,
    brief={
        "company": "Qalam AI", "role": "LLM Application Developer",
        "seniority": "junior", "tone": "scrappy",
        "role_narrative": (
            "Build retrieval pipelines that answer questions over support "
            "documents. Choose embedding models, run the Pinecone index, and "
            "measure every prompt change with evaluation sets."),
        "problems_to_solve": [
            {**g, "priority": "core"} for g in _grounded(
                QALAM,
                "Build retrieval pipelines over support documents and "
                "help-centre articles",
                "Write evaluation sets so every change to a prompt is "
                "measured")],
        "success_signals": _grounded(
            QALAM, "Build small internal tools with Python and Ollama for "
                   "local testing"),
        "hard_requirements": _grounded(
            QALAM, "Python",
            "Experience with embeddings and vector search (Pinecone, FAISS "
            "or similar)",
            "Experience with LlamaIndex or LangChain",
            "Some experience running open models locally"),
        "terms": [
            _term("Python", "skill", 9),
            _term("FAISS", "tool", 7),
            _term("Pinecone", "tool", 7),
            _term("sentence-transformers", "tool", 6, required=False,
                  aliases=("embeddings",)),
            _term("Ollama", "tool", 6),
            _term("LangChain", "tool", 5),
            _term("LlamaIndex", "tool", 5, required=False),
        ],
    },
    plan={"ops": [
        {"op": "set_summary",
         "text": "Computer science graduate who builds question answering "
                 "over documents with embeddings and FAISS, and runs open "
                 "models locally with Ollama.",
         "rationale": "Retrieval over documents and local models are the two "
                      "halves of this role, and the résumé has both.",
         "cites": ["exp.1.b.1", "prj.4.b.1"]},
        # ★ refused: Pinecone is the posting's tool, not the candidate's.
        {"op": "rewrite_bullet", "bullet_id": "exp.1.b.1", "text": "",
         "target_terms": ["Pinecone"],
         "rationale": "Say the retrieval work in the vocabulary of this "
                      "team's stack."},
        {"op": "rewrite_bullet", "bullet_id": "prj.4.b.1", "text": "",
         "target_terms": ["Ollama"],
         "rationale": "The posting asks for running open models locally; "
                      "this project is exactly that."},
        {"op": "flag_gap", "requirement": "Pinecone", "severity": "minor",
         "closest_evidence": ["exp.1.b.1"]},
        {"op": "flag_gap", "requirement": "LlamaIndex", "severity": "minor",
         "closest_evidence": []},
    ]},
    written={"bullets": [
        {"bullet_id": "exp.1.b.1",
         "text": "Built a document question-answering prototype over 1,200 "
                 "internal PDFs using sentence-transformers embeddings and a "
                 "Pinecone vector index"},
        {"bullet_id": "prj.4.b.1",
         "text": "Built a Python tool that runs a Llama model locally "
                 "through Ollama to summarise meeting audio transcribed with "
                 "Whisper"},
    ]},
    outreach={
        "subject": "Application: LLM Application Developer — Zara Ahmed, "
                   "retrieval and local models",
        "body": (
            "I am applying for the LLM Application Developer role at Qalam AI. "
            "Answering questions over documents is the problem I worked on in "
            "my internship, and running open models locally is how I built my "
            "own projects.\n\n"
            "At Datum Analytics I built a question-answering prototype over "
            "1,200 internal PDFs with sentence-transformers embeddings and "
            "FAISS, and wrote an evaluation script that compared three prompt "
            "templates on 150 labelled questions. Separately, I built a Python "
            "tool that summarises meeting audio with a local Llama model "
            "through Ollama.\n\n"
            "I have used FAISS rather than Pinecone, and I have not yet worked "
            "with LlamaIndex.\n\n"
            "Thank you for your time. My résumé is attached as a PDF, and I "
            "would be glad to talk about the role."),
        "cites": ["exp.1.b.1", "exp.1.b.2", "prj.4.b.1"],
    },
    refused=("unsupported_entity",),
    gaps=("Pinecone", "LlamaIndex"),
)

# ══ 4 · Indus Retail Analytics — keyword stuffing ════════════════════

INDUS = """Data Scientist — Indus Retail Analytics
Lahore · Full-time

We help retailers across Punjab understand what sells, where and why. You would join a team of four data scientists working on demand forecasting and customer segmentation.

What you will do
- Clean and model sales and customer data in Python and SQL
- Build and evaluate machine learning models with scikit-learn
- Present findings in Tableau dashboards for store managers
- Design and read A/B tests for promotions

What we need
- Python, pandas and SQL
- scikit-learn
- A careful approach to data quality and labelling
- Tableau or Power BI
- Experience with A/B testing

Apply with your CV at talent@indusretail.example.
"""

CASE_INDUS = Case(
    key="indus",
    what_it_shows="A tool list appended to an honest bullet is refused as "
                  "keyword stuffing, even though every tool is on the résumé.",
    posting=INDUS,
    brief={
        "company": "Indus Retail Analytics", "role": "Data Scientist",
        "seniority": "junior", "tone": "pragmatic",
        "role_narrative": (
            "Clean and model retail sales and customer data in Python and "
            "SQL, build scikit-learn models for forecasting and segmentation, "
            "and present findings to store managers in Tableau."),
        "problems_to_solve": [
            {**g, "priority": "core"} for g in _grounded(
                INDUS,
                "Clean and model sales and customer data in Python and SQL",
                "Build and evaluate machine learning models with "
                "scikit-learn")],
        "success_signals": _grounded(
            INDUS, "A careful approach to data quality and labelling"),
        "hard_requirements": _grounded(
            INDUS, "Python, pandas and SQL", "scikit-learn",
            "Tableau or Power BI", "Experience with A/B testing"),
        "terms": [
            _term("Python", "skill", 9),
            _term("SQL", "skill", 8),
            _term("pandas", "tool", 7),
            _term("scikit-learn", "tool", 7, aliases=("sklearn",)),
            _term("Tableau", "tool", 6, aliases=("Power BI",)),
            _term("A/B testing", "skill", 6, aliases=("A/B tests",)),
        ],
    },
    plan={"ops": [
        {"op": "rewrite_bullet", "bullet_id": "exp.1.b.2", "text": "",
         "target_terms": [],
         "rationale": "Evaluation against labelled data is the careful "
                      "habit this posting asks for; lead with the method."},
        # ★ refused: five tool names bolted onto the end of a real bullet.
        {"op": "rewrite_bullet", "bullet_id": "exp.1.b.4", "text": "",
         "target_terms": ["Python", "pandas", "scikit-learn", "SQL"],
         "rationale": "Show the tools the posting lists."},
        {"op": "set_summary",
         "text": "Computer science graduate who cleans and labels data and "
                 "evaluates models against labelled test sets, in Python.",
         "rationale": "Data quality and evaluation are what this posting "
                      "asks for first, and the résumé shows both.",
         "cites": ["exp.1.b.4", "exp.1.b.2", "prj.3.b.1"]},
        {"op": "flag_gap", "requirement": "Tableau or Power BI",
         "severity": "major", "closest_evidence": ["prj.3.b.2"]},
        {"op": "flag_gap", "requirement": "A/B testing", "severity": "major",
         "closest_evidence": []},
    ]},
    written={"bullets": [
        {"bullet_id": "exp.1.b.2",
         "text": "Compared three prompt templates against 150 labelled "
                 "questions with an evaluation script that reported answer "
                 "accuracy for each template"},
        {"bullet_id": "exp.1.b.4",
         "text": "Helped clean and label training data for a support-ticket "
                 "classification model using Python, pandas, scikit-learn, "
                 "SQL and PyTorch"},
    ]},
    outreach={
        "subject": "Application: Data Scientist — Zara Ahmed, model "
                   "evaluation and data labelling",
        "body": (
            "I am applying for the Data Scientist role at Indus Retail "
            "Analytics. Your posting asks for a careful approach to data "
            "quality, and most of my practical work has been about measuring "
            "whether a model is right.\n\n"
            "During my internship I helped clean and label training data for "
            "a support-ticket classification model, and wrote an evaluation "
            "script that compared three prompt templates on 150 labelled "
            "questions. For my final-year project I fine-tuned a classifier "
            "that reached 87% accuracy on a held-out test set.\n\n"
            "I have not yet worked with Tableau or designed A/B tests, and I "
            "would want to learn both from your team.\n\n"
            "Thank you for your time. My résumé is attached as a PDF, and I "
            "would welcome a short conversation about the role."),
        "cites": ["exp.1.b.2", "exp.1.b.4", "prj.3.b.1"],
    },
    refused=("keyword_stuffing",),
    gaps=("Tableau", "A/B testing"),
)

# ══ 5 · Rahbar Fintech — the honest case ═════════════════════════════

RAHBAR = """Python Backend Engineer (AI services) — Rahbar Fintech
Islamabad · Full-time

Rahbar builds lending tools for small businesses. Our AI team serves document-understanding models to the rest of the company through internal APIs.

You will
- Build and maintain FastAPI services that wrap our models
- Package services in Docker and keep them tested
- Write automated tests for every endpoint
- Work with PostgreSQL for request logging and results

We are looking for
- Python and FastAPI
- Docker
- Automated testing
- PostgreSQL
- Experience with Celery and Redis for background jobs

Please apply at careers@rahbar.example.
"""

CASE_RAHBAR = Case(
    key="rahbar",
    what_it_shows="Nothing refused: every edit is supported by a line the "
                  "candidate wrote, and the gaps are reported, not filled.",
    posting=RAHBAR,
    brief={
        "company": "Rahbar Fintech",
        "role": "Python Backend Engineer (AI services)",
        "seniority": "junior", "tone": "formal",
        "role_narrative": (
            "Build and maintain the FastAPI services that serve the AI team's "
            "document models to the rest of the company, packaged in Docker, "
            "tested at every endpoint and logged to PostgreSQL."),
        "problems_to_solve": [
            {**g, "priority": "core"} for g in _grounded(
                RAHBAR,
                "Build and maintain FastAPI services that wrap our models",
                "Write automated tests for every endpoint")],
        "success_signals": _grounded(
            RAHBAR, "Package services in Docker and keep them tested"),
        "hard_requirements": _grounded(
            RAHBAR, "Python and FastAPI", "Docker", "Automated testing",
            "PostgreSQL",
            "Experience with Celery and Redis for background jobs"),
        "terms": [
            _term("Python", "skill", 9),
            _term("FastAPI", "tool", 9),
            _term("Docker", "tool", 8),
            _term("automated testing", "skill", 7,
                  aliases=("automated test cases", "test cases")),
            _term("PostgreSQL", "tool", 6, aliases=("postgres",)),
            _term("Celery", "tool", 5),
            _term("Redis", "tool", 5),
        ],
    },
    plan={"ops": [
        {"op": "set_summary",
         "text": "Computer science graduate who serves machine learning "
                 "models as FastAPI services in Docker, and writes automated "
                 "tests for the code she ships.",
         "rationale": "The role is serving models behind tested APIs; the "
                      "résumé has the FastAPI service, the Docker packaging "
                      "and the automated tests, spread across two roles.",
         "cites": ["exp.1.b.3", "exp.2.b.2"]},
        {"op": "reorder_bullets", "item_id": "exp.1",
         "order": ["exp.1.b.3", "exp.1.b.1", "exp.1.b.2", "exp.1.b.4"],
         "rationale": "Put the FastAPI service first; it is this posting's "
                      "first line."},
        {"op": "rewrite_bullet", "bullet_id": "exp.1.b.3", "text": "",
         "target_terms": ["FastAPI", "Docker"],
         "rationale": "Say what the service wrapped, since the role is "
                      "wrapping models."},
        {"op": "rewrite_bullet", "bullet_id": "exp.2.b.2", "text": "",
         "target_terms": [],
         "rationale": "Make the testing work read as testing work."},
        {"op": "flag_gap", "requirement": "Celery and Redis",
         "severity": "minor", "closest_evidence": []},
    ]},
    written={"bullets": [
        {"bullet_id": "exp.1.b.3",
         "text": "Packaged the question-answering prototype as a FastAPI "
                 "service in Docker so the support team could test it"},
        {"bullet_id": "exp.2.b.2",
         "text": "Wrote the automated test cases used to check student lab "
                 "submissions"},
    ]},
    outreach={
        "subject": "Application: Python Backend Engineer (AI services) — "
                   "Zara Ahmed, FastAPI and Docker",
        "body": (
            "I am applying for the Python Backend Engineer (AI services) role "
            "at Rahbar Fintech. Serving a model behind a FastAPI service in "
            "Docker is the part of my internship I would most like to do "
            "every day.\n\n"
            "At Datum Analytics I packaged a document question-answering "
            "prototype as a FastAPI service in Docker so the support team "
            "could test it. As a teaching assistant I wrote the automated test "
            "cases used to check lab submissions, which is where my habit of "
            "testing every change comes from.\n\n"
            "I have not yet used Celery or Redis for background jobs.\n\n"
            "Thank you for your time. My résumé is attached as a PDF, and I "
            "would welcome a short conversation about the role."),
        "cites": ["exp.1.b.3", "exp.2.b.2"],
    },
    refused=(),
    gaps=("Celery", "Redis"),
)

CASES: tuple[Case, ...] = (CASE_MERIDIAN, CASE_SADAF, CASE_QALAM, CASE_INDUS,
                           CASE_RAHBAR)


# ── running and checking ──────────────────────────────────────────────

def run_case(case: Case) -> RunState:
    from evals.harness import ScriptedClient
    return tailor(normalize(RESUME), case.posting, ScriptedClient(case.answers),
                  write_outreach=True, render_output=True)


def verify(case: Case, state: RunState) -> list[str]:
    """Everything this case claims on screen, checked. Empty means seedable."""
    problems: list[str] = []
    original = normalize(RESUME)

    codes = sorted(r.code for r in state.rejected)
    if codes != sorted(case.refused):
        problems.append(f"refused {codes}, expected {sorted(case.refused)}: "
                        f"{[r.detail for r in state.rejected]}")

    applied = [op for op in state.accepted
               if op.op not in ("flag_gap", "ask_user")]
    if not applied:
        problems.append("no change was applied — a demo of nothing")

    # The central claim, checked on the output rather than trusted: every
    # number in the tailored résumé is a number on the original.
    def all_numbers(doc) -> set[str]:
        found = numbers(doc.summary)
        for bullet in doc.all_bullets():
            found |= numbers(bullet.text)
        return found
    if invented := all_numbers(state.tailored) - all_numbers(original):
        problems.append(f"tailored résumé has numbers not on the original: "
                        f"{sorted(invented)}")

    proofs = proof_checks(state)
    if failed := [name for name, ok in proofs.items() if not ok]:
        problems.append(f"proof checks failed: {failed}")

    standing = state.standing or {}
    missing = {t.lower() for t in standing.get("not_found", [])}
    declared = {t.lower() for t in standing.get("declared_only", [])}
    for gap in case.gaps:
        if gap.lower() not in missing | declared:
            problems.append(f"{gap!r} should stand as a gap; standing is "
                            f"{standing}")

    outreach = state.outreach
    if outreach is None:
        problems.append("no email drafted")
    else:
        if outreach.problems:
            problems.append(f"email problems: {outreach.problems}")
        if not outreach.body.startswith("Dear Hiring Team,\n\n"):
            problems.append("email does not open 'Dear Hiring Team,'")
        if not outreach.body.endswith("Kind regards,\nZara Ahmed"):
            problems.append("email is not signed 'Kind regards, Zara Ahmed'")
        if not outreach.recipient:
            problems.append("no recipient found in the posting")

    if state.rendered is None or not state.rendered.is_clean:
        checks = [c.summary() for c in state.rendered.checks] \
            if state.rendered else []
        problems.append(f"render not clean: {checks}")
    return problems


# ── persistence ───────────────────────────────────────────────────────

def _resume(session, user, document) -> models.Resume:
    text = "\n".join(
        [document.contact.full_name]
        + [b.text for s in document.sections for i in s.items for b in i.bullets])
    resume = models.Resume(
        user_id=user.id, version=1, filename="Zara_Ahmed_CV.pdf",
        file_sha256=hashlib.sha256(text.encode()).hexdigest(),
        file_bytes=len(text.encode()), extract_text=text,
        raw_json=RESUME.model_dump(), doc_json=document.model_dump(),
        coverage_json={"dropped": [], "invented": [], "structure": []},
        status="confirmed", revision=1,
        confirmed_at=datetime.now(timezone.utc))
    session.add(resume)
    session.flush()
    return resume


def _run(session, user, resume, case: Case, state: RunState) -> models.Run:
    """`demo_seed._run`, with this case's posting, and the seeded proof block
    carrying the checks this module ran."""
    run = demo_seed._run(session, user, resume, state)
    run.jd_text = case.posting
    run.jd_sha256 = hashlib.sha256(case.posting.encode()).hexdigest()
    run.proof_json = {**proof_checks(state), "seeded": True, "model_calls": 0,
                      "showcase": case.key}
    session.flush()
    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="run and verify every case; write nothing")
    parser.add_argument("--reset", action="store_true",
                        help="accepted for compatibility; seeding always "
                             "replaces the demo account")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")

    results = []
    for case in CASES:
        state = run_case(case)
        problems = verify(case, state)
        results.append((case, state, problems))
        mark = "PASS" if not problems else "FAIL"
        refused = ", ".join(r.code for r in state.rejected) or "nothing"
        changes = len(state.diff.changes) if state.diff else 0
        print(f"{mark}  {case.key:9} {changes} change(s), refused: {refused}")
        for p in problems:
            print(f"        ✗ {p}")

    if any(problems for _, _, problems in results):
        print("\nNot seeding: every case must pass its own check first.")
        return 1
    if args.check:
        print("\nAll five pass. Nothing written (--check).")
        return 0

    with session_scope() as session:
        demo_seed._remove_existing(session)
        user = demo_seed._user(session)
        user.name = "Zara Ahmed (demo)"
        resume = _resume(session, user, normalize(RESUME))
        for case, state, _ in results:
            run = _run(session, user, resume, case, state)
            demo_seed._artifacts(session, run, state)
            print(f"seeded {case.key:9} /runs/{run.id}")
    print(f"\nSeeded five applications for {demo_seed.DEMO_EMAIL}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
