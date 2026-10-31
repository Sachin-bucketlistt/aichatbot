"""Fixed WhatsApp price-and-details message (data/price_overview.md), sent word for word, no LLM call.

Used when a WhatsApp customer asks for the whole price list or "everything you offer". Edit the markdown
file to change it (restart to reload). A question about ONE activity ("bungee price?") is not matched and
goes to the model as usual. Prices in the file are typed by hand: update them when the catalog changes.
"""
import logging
import re
from functools import lru_cache
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

MAX_ASK_CHARS = 300   # a long message is almost always a specific question

# Matched on the text with whitespace collapsed to single spaces, so the patterns need no \s+ (linear time).
_STRONG = re.compile(
    r"\b(?:price|prices|rate|rates|pricing|cost|package|packages) (?:list|chart|card|range|ranges|details)\b|"
    r"\b(?:rate|price) (?:card|list|chart)\b|"
    r"\ball (?:the )?(?:prices|rates|activities|packages|options|things|details)\b|"
    r"\b(?:prices|rates|details) (?:of|for) (?:all|everything)\b|"
    r"\b(?:everything|all) (?:you|that you|u) (?:offer|have|provide)\b|"
    r"\bwhat (?:all )?(?:activities|things) (?:do )?(?:you|u) (?:offer|have|provide)\b|"
    r"\b(?:sab|sabhi|saare|sare) [a-z ]{0,20}(?:price|prices|rate|rates|activities)\b|"
    r"\bfull (?:price|details)\b|\bcomplete (?:price|details)\b",
    re.IGNORECASE,
)
# A general ask: a price/options word with no single activity named (or two or more named), and no specifics
# (a height or distance, a named jump, "book", a date, a head-count). "bungee prices?" or "price of 111m"
# stay with the model, which answers for that one activity.
_ASK = re.compile(
    r"\b(?:price|prices|pricing|rate|rates|cost|costs|charges|fees|kimat|kitna|kitne|options|activities|"
    r"details|detail|offers|list|info|information)\b",
    re.IGNORECASE,
)
_SPECIFIC = re.compile(
    r"\b\d+ ?(?:m|km|meter|metre|meters|metres)\b|thrill factory|splash|jumpin|himalayan|ganga|drone|freestyle|"
    r"\bbook|\bdate\b|today|tomorrow|weekend|\bpeople\b|\bpersons\b|\bfor \d|\b\d+ (?:of us|persons?|people|log)\b",
    re.IGNORECASE,
)
_ACTIVITIES = (
    re.compile(r"bung|jump", re.IGNORECASE), re.compile(r"raft", re.IGNORECASE), re.compile(r"paraglid", re.IGNORECASE),
    re.compile(r"zipline|flying fox", re.IGNORECASE), re.compile(r"camp", re.IGNORECASE),
    re.compile(r"balloon", re.IGNORECASE), re.compile(r"paramotor", re.IGNORECASE), re.compile(r"swing", re.IGNORECASE),
)
MAX_USER_MESSAGES = 3   # only early in a chat: later "what's the price?" is a booking-stage question
SENT_MARKER = "Welcome to Bucketlistt!"   # first line of the message: never send it twice in one chat


def wants_price_overview(text: str) -> bool:
    text = " ".join(text.split())
    if not text or len(text) > MAX_ASK_CHARS:
        return False
    if _STRONG.search(text):
        return True
    if not _ASK.search(text) or _SPECIFIC.search(text):
        return False
    return sum(1 for r in _ACTIVITIES if r.search(text)) != 1


def should_send_overview(user_texts: list[str], assistant_texts: list[str]) -> bool:
    """Latest user message is a general price/options ask, early in the chat, and the message was not sent yet."""
    return (bool(user_texts) and len(user_texts) <= MAX_USER_MESSAGES and wants_price_overview(user_texts[-1])
            and not any(SENT_MARKER in t for t in assistant_texts))


@lru_cache(maxsize=1)
def price_overview_text() -> str:
    """The message, or "" if the file is missing (then nothing is short-circuited)."""
    path = Path(settings.system_prompt_file).parent / "price_overview.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        logger.warning("Price overview file missing (%s): the fixed price message is disabled", path)
        return ""
