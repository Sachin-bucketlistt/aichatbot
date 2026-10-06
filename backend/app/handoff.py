"""Human-handoff support for the CRM endpoint (POST /api/v2/chat).

The model signals a handoff by calling HANDOFF_TOOL; detect_handoff() is a
deterministic backstop on the customer's own words, so an obvious "get me a
human" still reaches the CRM if the model forgets to call the tool. Both are
only wired in when a caller passes handoff_state (see app/llm.py) — the web
chat at /api/chat never sees this tool.
"""
import re

HANDOFF_TOOL = "request_human_handoff"

HANDOFF_REASONS = [
    "customer_requested_human",
    "cannot_answer",
    "complaint",
    "payment_issue",
    "booking_change",
    "other",
]

HANDOFF_SCHEMA = {
    "type": "function",
    "function": {
        "name": HANDOFF_TOOL,
        "description": (
            "Pass this conversation to a human salesperson. Call it when the customer asks for a "
            "person, you cannot answer, they complain, report a payment problem, or want to change "
            "or cancel an existing booking. After calling it, reply with ONE short friendly sentence "
            "saying the team will follow up here. Do not use escalate_and_capture_lead for this."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "enum": HANDOFF_REASONS},
            },
            "required": ["reason"],
        },
    },
}

HANDOFF_PROMPT = (
    "## Human handoff\n"
    "Earlier 'assistant' messages may have been written by human staff, not you; do not contradict "
    "or repeat them. When the customer needs a person (see the request_human_handoff tool), call "
    "that tool instead of escalate_and_capture_lead."
)

_BACKSTOP = [
    ("payment_issue", re.compile(r"refund|charged twice|payment (failed|issue|problem)|money (deducted|debited)", re.I)),
    ("complaint", re.compile(r"complain|terrible|worst|scam|fraud|disgust|unacceptable", re.I)),
    ("customer_requested_human", re.compile(
        r"\b(human|real person|live agent|representative|customer (care|support|service)|manager)\b|"
        r"\b(talk|speak|connect me) (to|with) (an? )?(agent|person|human|someone|team)",
        re.I,
    )),
]


def detect_handoff(text: str) -> str | None:
    """Reason code if the customer's message plainly calls for a human, else None."""
    for reason, pattern in _BACKSTOP:
        if pattern.search(text or ""):
            return reason
    return None
