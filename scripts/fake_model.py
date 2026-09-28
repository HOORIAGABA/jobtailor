"""An OpenAI-compatible model server that answers from the demo script.

For driving the REAL product — the API, the UI, upload, confirm, run, gate,
send — without a GPU or a key. It speaks exactly the protocol
`io.llm.OpenAICompatibleClient` speaks (`POST /v1/chat/completions` with a
`response_format` carrying the stage's JSON schema), and answers each stage
with the stored demo answer. Every deterministic stage — extraction, the
coverage check, evidence matching, the validator, apply, diff, render — runs
for real, so the refusal on the gate screen is the real guard refusing a real
fabricated number.

    python -m scripts.fake_model            # listens on :11434, like Ollama

    LLM_PROVIDER=ollama
    LLM_BASE_URL=http://127.0.0.1:11434/v1
    LLM_MODEL=demo

It is not a test double for the model's *judgment* — it is the same scripted
answer every time. It exists so the product's plumbing can be exercised end to
end through a browser, which is how F1 (`POST /api/runs` answered 500 on every
request) would have been caught before it shipped.
"""
from __future__ import annotations

import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from evals.harness import _stage_of
from scripts.demo_seed import ANSWERS, RESUME

logger = logging.getLogger("fake_model")


def parse_answer() -> dict[str, Any]:
    """The demo résumé as the parse stage's `ResumeDraft`.

    The document-level summary becomes a SUMMARY section, because that is
    the only place `engine.normalize` derives a summary from — the draft
    schema has no top-level summary on purpose. It goes LAST: item ids are a
    running counter across sections, and the scripted plan cites the ids the
    seed produced (`exp.1.b.2`, `prj.3.b.1`). A summary section in front
    would shift every id by one and turn the scripted refusal into
    `unknown_id`.
    """
    sections = []
    for section in RESUME.sections:
        sections.append({
            "heading": section.heading,
            "entries": [{"title": e.title, "org": e.org, "dates": e.dates,
                         "bullets": list(e.bullets)} for e in section.entries],
        })
    sections.append({"heading": "SUMMARY", "entries": [
        {"title": "", "org": "", "dates": "", "bullets": [RESUME.summary]}]})
    return {"contact": RESUME.contact.model_dump(), "sections": sections}


def answer_for(body: dict[str, Any]) -> Any:
    fmt = body.get("response_format") or {}
    schema = (fmt.get("json_schema") or {}).get("schema")
    stage = _stage_of(schema)
    if stage == "parse":
        return parse_answer()
    if stage in ANSWERS:
        return ANSWERS[stage]
    raise KeyError(f"no scripted answer for stage {stage!r}")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:                                  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        try:
            answer = answer_for(body)
        except KeyError as exc:
            self._send(400, {"error": {"message": str(exc)}})
            return
        text = json.dumps(answer)
        logger.info("%s -> %s (%d chars)", self.path,
                    _stage_of(((body.get("response_format") or {})
                               .get("json_schema") or {}).get("schema")),
                    len(text))
        self._send(200, {
            "id": "chatcmpl-demo", "object": "chat.completion",
            "model": body.get("model", "demo"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 400, "completion_tokens": len(text) // 4,
                      "total_tokens": 400 + len(text) // 4},
        })

    def do_GET(self) -> None:                                   # noqa: N802
        self._send(200, {"data": [{"id": "demo", "object": "model"}]})

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_: Any) -> None:                     # quiet
        return


def main(argv: list[str] | None = None) -> int:
    port = int((argv or sys.argv[1:] or ["11434"])[0])
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    logger.info("fake model on http://127.0.0.1:%d/v1 — answers from the demo "
                "script, no judgment involved", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
