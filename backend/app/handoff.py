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

# A customer asking WHAT the bot is ("are you a bot / real person?") is not asking for a human.
_IDENTITY_QUESTION = re.compile(
    r"\bare you (?:a |an |the )?(?:real|human|bot|robot|ai|person|machine)\b|"
    r"\b(?:kya )?aap (?:insaan|insan|bot|robot|human|real)\b|\b(?:insaan|bot|robot) (?:ho|hai|hain)\b",
    re.I,
)

_BACKSTOP = [
    ("payment_issue", re.compile(
        r"refund|charged twice|payment (failed|issue|problem)|money (deducted|debited)|"
        r"paise? (kat|cut|deduct)|payment nahi", re.I)),
    # Narrow on purpose: a bare "terrible" / "worst" shows up in casual lines ("worst case if it rains?").
    ("complaint", re.compile(
        r"complain|shikayat|scam|fraud|dhokha|cheat(?:ed|ing)|disgust|unacceptable|"
        r"(?:worst|terrible|horrible|pathetic|bekar|bakwas) (?:service|experience|company|support|behaviou?r)|"
        r"bahut bura", re.I)),
    ("customer_requested_human", re.compile(
        r"\b(human|real person|live agent|representative|customer (care|support|service))\b|"
        r"\b(talk|speak|connect me|chat) (to|with) (an? |the |your )?(agent|person|human|someone|team|manager|owner)\b|"
        # Hindi / Hinglish: "team se baat karni hai", "insaan se baat", "manager se baat"
        r"\b(team|insaan|insan|human|manager|owner|agent|kisi|koi)\s+(se|ko)\s+(baat|bol|bat)|"
        r"\bbaat\s+(karni|karna|karwa|kara)\w*\s+(hai\s+)?(team|insaan|kisi|manager|agent)|"
        # a callback request is also a handoff: the bot cannot call
        r"\bcall\s*(me|back)\b|\bcallback\b|\bgive me a call\b|\bmujhe call\b|"
        r"\bcall\s*(kar|karo|karna|karein|dena|kijiye|kariye)\b|\bphone\s*(kar|karo|karna)\b",
        re.I,
    )),
]


def detect_handoff(text: str) -> str | None:
    """Reason code if the customer's message plainly calls for a human, else None."""
    if _IDENTITY_QUESTION.search(text or ""):
        return None
    for reason, pattern in _BACKSTOP:
        if pattern.search(text or ""):
            return reason
    return None
