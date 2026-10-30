"""Human-handoff support for the CRM endpoint (POST /api/v2/chat).

The model signals a handoff by calling HANDOFF_TOOL; detect_handoff() is a
deterministic backstop on the customer's own words, so an obvious "get me a
human" still reaches the CRM if the model forgets to call the tool. Both are
only wired in when a caller passes handoff_state (see app/llm.py) — the web
chat at /api/chat never sees this tool.
"""
import re

HANDOFF_TOOL = "request_human_handoff"

# Given ONLY to a customer who asks for a discount because they are Gujarati. The code is in the prompt
# only for such a chat (see asks_gujarati_discount), so the model cannot offer it upfront.
GUJARATI_COUPON = "GUJARATI5"

HANDOFF_REASONS = [
    "customer_requested_human",
    "cannot_answer",
    "complaint",
    "payment_issue",
    "booking_change",
    "discount_request",
    "other",
]

HANDOFF_SCHEMA = {
    "type": "function",
    "function": {
        "name": HANDOFF_TOOL,
        "description": (
            "Pass this conversation to a human salesperson. Call it when the customer asks for a "
            "person, you cannot answer, they complain, report a payment problem, want to change "
            "or cancel an existing booking, or ask for an extra discount or a lower price (reason discount_request). After calling it, reply with ONE short friendly sentence "
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
    "that tool instead of escalate_and_capture_lead.\n"
    "### Discount requests\n"
    "When the customer asks or pushes for an extra discount, a special or lower price, a coupon or a "
    "better deal (including 'Gujarati/student/group discount' or 'last price'): never say no, never say "
    "you cannot or do not give discounts, and never invent, offer or promise one (the only exception is a "
    "coupon given to you in a separate Gujarati instruction). Reply in their language "
    "in at most two short sentences: the prices shown are already discounted, and you will pass this to a "
    "teammate who can decide on it. Then call request_human_handoff with reason discount_request in the "
    "same turn. Only for a real request to pay less: a question about the listed or discounted price is "
    "answered normally, with no handoff."
)

GUJARATI_DISCOUNT_PROMPT = (
    "## Gujarati customer discount — this customer asked for a discount because they are Gujarati\n"
    f"Tell them warmly that there is a special discount for Gujarati customers: apply coupon {GUJARATI_COUPON} "
    "at checkout for 5% extra off, on top of the already-discounted prices. One or two short sentences, in "
    "their language. Then call request_human_handoff with reason discount_request in the same turn, so a "
    "teammate can help further. Offer no other discount and never change the 5%. Do not mention this to "
    "anyone who has not asked for it as a Gujarati."
)

_GUJARATI = re.compile(r"\bguj[a-z]*", re.I)
_DISCOUNT_WORD = re.compile(
    r"discount|offer|coupon|concession|rebate|sast[ai]|cheaper|kam\s*kar|special\s+(?:rate|price)", re.I)


def asks_gujarati_discount(user_texts: list[str]) -> bool:
    """True if any single customer message asks for a discount as a Gujarati
    ("Gujarati ke liye special discount"). Not just mentioning Gujarat, and not the (GJ) state tag."""
    return any(_GUJARATI.search(t) and _DISCOUNT_WORD.search(t) for t in user_texts)


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
    # A request to pay less. Not bare "discounted": a question about the listed price is no handoff.
    ("discount_request", re.compile(
        r"\b(?:extra|more|special|additional|further|better|any|koi|kuch|student|group|bulk|gujarati?|"
        r"couple|birthday|early\s*bird)\s+(?:discounts?|offers?|deals?|rates?|price)\b|"
        r"\bdiscounts?\b.{0,25}\b(?:milega|mil\s*sakta|mil\s*jayega|do|dedo|de\s*do|de\s*sakte|chahiye|"
        r"hoga|hai|possible|available|please|plz|pls|dijiye|kijiye|can|cant)\b|^\W*(?:any\s+)?discounts?\W*$|"
        r"\b(?:price|rate|rates|daam|bhav|kimat)\s+(?:thoda\s+|kuch\s+|bit\s+)?(?:kam|less|lower|reduce)\b|"
        r"\b(?:kam|reduce|lower|decrease)\s+(?:kar|karo|kardo|kar\s*do|karna|kijiye|the\s+(?:price|rate)|"
        r"(?:price|rate))\b|\b(?:last|final|best|lowest)\s+(?:price|rate)\b|"
        r"\bbargain|\bnegotiat|\bhaggl|\bcoupons?\b|\bpromo\s*codes?\b", re.I)),
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
