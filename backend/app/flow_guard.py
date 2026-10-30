# backend/app/flow_guard.py
"""Signals that mean 'this message is mid-flow or safety-sensitive — never let
any content-based routing shortcut skip the full pipeline for it.' Vocabulary
is pulled directly from knowledge_base.md's own Safety Reassurance (171-172)
and Medical Contraindications (276-294) sections so the two lists can't drift
out of sync with each other.
"""
import re

_OTP_RE = re.compile(r"^\s*\d{4,8}\s*$")
_PHONE_RE = re.compile(r"^\s*[+]?\d[\d\s-]{7,14}\d\s*$")
_SAFETY_WORDS = re.compile(
    r"scared|nervous|safe|safety|certified|insurance|heart|pregnant|"
    r"age limit|weight limit|medical|doctor|cord|harness|accident|injury|"
    r"die|died|death|risk",
    re.IGNORECASE,
)


def is_hard_protected(latest_user_message: str) -> bool:
    """OTP codes, phone numbers and safety talk only. Unlike is_protected_turn this does NOT treat a
    short message ("bungee price", "rafting") as protected, so it can still reach the live catalog."""
    text = latest_user_message.strip()
    return bool(_OTP_RE.match(text) or _PHONE_RE.match(text) or _SAFETY_WORDS.search(text))


def is_protected_turn(latest_user_message: str) -> bool:
    """True if this message must always reach the full, unrestricted pipeline —
    never short-circuited by a tool-choice or (any future) content gate."""
    text = latest_user_message.strip()
    if _OTP_RE.match(text) or _PHONE_RE.match(text):
        return True
    if len(text.split()) <= 3:
        return True  # short reply/continuation — "yes", "sounds good", etc.
    if _SAFETY_WORDS.search(text):
        return True
    return False
