"""Fixed WhatsApp price-and-details message (data/price_overview.md), sent word for word, no LLM call.

Used when a WhatsApp customer asks for the whole price list or "everything you offer". Edit the markdown
file to change it (restart to reload). A question about ONE activity ("bungee price?") is not matched and
goes to the model as usual. Prices in the file are typed by hand: update them when the catalog changes.
"""
import re
from functools import lru_cache
from pathlib import Path

from app.config import settings

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
_WEAK = re.compile(r"\b(?:your|the|ur) (?:prices|rates|pricing)\b|\bwhat are (?:the )?(?:prices|rates)\b", re.IGNORECASE)
_ONE_ACTIVITY = re.compile(r"bung|raft|paraglid|zipline|flying fox|camp|balloon|paramotor|swing|jump", re.IGNORECASE)


def wants_price_overview(text: str) -> bool:
    text = " ".join(text.split())
    if not text or len(text) > MAX_ASK_CHARS:
        return False
    if _STRONG.search(text):
        return True
    return bool(_WEAK.search(text)) and not _ONE_ACTIVITY.search(text)


@lru_cache(maxsize=1)
def price_overview_text() -> str:
    """The message, or "" if the file is missing (then nothing is short-circuited)."""
    path = Path(settings.system_prompt_file).parent / "price_overview.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
