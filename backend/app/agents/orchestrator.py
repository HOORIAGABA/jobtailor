"""
The Agent Orchestrator (section 4 of the blueprint), implemented with
LangGraph. Each pipeline stage is a node with a single responsibility;
state is threaded through as a typed dict.

This is an agentic loop, not a one-pass chain: the tailor and outreach
nodes are followed by reflection (review) nodes. If a reviewer flags
problems, the graph routes BACK to the producer node (bounded retries)
with the reviewer's feedback, giving the pipeline a
plan -> execute -> review -> revise cycle per stage.
"""
from typing import TypedDict, Optional
from langgraph.graph import StateGraph, END

from app.agents.job_parser_agent import parse_job_post
from app.agents.tailoring_agent import tailor_resume
from app.agents.outreach_agent import draft_outreach_message
from app.agents.review_agent import review_resume, review_message

MAX_RESUME_REVISIONS = 2
MAX_MESSAGE_REVISIONS = 1


class PipelineState(TypedDict, total=False):
    job_raw_text: str
    resume_id: str
    resume_json: dict
    job_requirements: dict
    tailored_resume: dict
    draft_message: str
    error: Optional[str]

    # Agentic-loop state
    revision: int                       # resume revision count
    review_feedback: Optional[str]      # feedback fed back into tailor
    resume_review: Optional[dict]
    message_revision: int               # message revision count
    message_review_feedback: Optional[str]
    message_review: Optional[dict]


def node_parse_job(state: PipelineState) -> PipelineState:
    try:
        state["job_requirements"] = parse_job_post(state["job_raw_text"])
    except Exception as e:
        state["error"] = f"job parsing failed: {e}"
    return state


def route_after_parse(state: PipelineState) -> str:
    return "end" if state.get("error") else "tailor"


def node_tailor_resume(state: PipelineState) -> PipelineState:
    try:
        state["tailored_resume"] = tailor_resume(
            resume_id=state["resume_id"],
            resume_json=state["resume_json"],
            job_requirements=state["job_requirements"],
            feedback=state.get("review_feedback"),
        )
    except Exception as e:
        state["error"] = f"tailoring failed: {e}"
    return state


def node_review_resume(state: PipelineState) -> PipelineState:
    try:
        review = review_resume(
            job_requirements=state["job_requirements"],
            tailored_resume=state["tailored_resume"],
            original_resume=state.get("resume_json"),
        )
    except Exception as e:
        review = {"needs_revision": False, "issues": [f"review failed: {e}"], "recommendations": [], "feedback": ""}
    state["resume_review"] = review
    state["revision"] = state.get("revision", 0) + 1
    state["review_feedback"] = review.get("feedback") or "\n".join(review.get("issues", []))
    return state


def route_after_review(state: PipelineState) -> str:
    review = state.get("resume_review") or {}
    if (
        review.get("needs_revision")
        and state.get("revision", 0) <= MAX_RESUME_REVISIONS
        and not state.get("error")
    ):
        return "tailor"
    return "outreach"


def node_draft_outreach(state: PipelineState) -> PipelineState:
    try:
        state["draft_message"] = draft_outreach_message(
            job_requirements=state["job_requirements"],
            tailored_resume=state["tailored_resume"],
            feedback=state.get("message_review_feedback"),
        )
    except Exception as e:
        state["error"] = f"outreach drafting failed: {e}"
    return state


def node_review_message(state: PipelineState) -> PipelineState:
    try:
        review = review_message(
            job_requirements=state["job_requirements"],
            draft_message=state["draft_message"],
        )
    except Exception as e:
        review = {"needs_revision": False, "issues": [f"review failed: {e}"], "recommendations": [], "feedback": ""}
    state["message_review"] = review
    state["message_revision"] = state.get("message_revision", 0) + 1
    state["message_review_feedback"] = review.get("feedback") or "\n".join(review.get("issues", []))
    return state


def route_after_message_review(state: PipelineState) -> str:
    review = state.get("message_review") or {}
    if (
        review.get("needs_revision")
        and state.get("message_revision", 0) <= MAX_MESSAGE_REVISIONS
        and not state.get("error")
    ):
        return "outreach"
    return "end"


def build_graph():
    graph = StateGraph(PipelineState)
    graph.add_node("parse_job", node_parse_job)
    graph.add_node("tailor", node_tailor_resume)
    graph.add_node("review_resume", node_review_resume)
    graph.add_node("outreach", node_draft_outreach)
    graph.add_node("review_message", node_review_message)

    graph.set_entry_point("parse_job")
    graph.add_conditional_edges("parse_job", route_after_parse, {"tailor": "tailor", "end": END})
    graph.add_edge("tailor", "review_resume")
    graph.add_conditional_edges("review_resume", route_after_review, {"tailor": "tailor", "outreach": "outreach"})
    graph.add_edge("outreach", "review_message")
    graph.add_conditional_edges("review_message", route_after_message_review, {"outreach": "outreach", "end": END})

    return graph.compile()


_compiled_graph = None


def run_pipeline(job_raw_text: str, resume_id: str, resume_json: dict) -> PipelineState:
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()

    initial_state: PipelineState = {
        "job_raw_text": job_raw_text,
        "resume_id": resume_id,
        "resume_json": resume_json,
    }
    return _compiled_graph.invoke(initial_state)