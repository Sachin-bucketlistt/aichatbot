"""State tag on a WhatsApp chat's first message, e.g. "Hi! I want to book bungee. (GJ)".

The CRM/ad flow appends a two-letter state code in brackets. It picks the reply language for the whole
chat (Roman script only, no native script): GJ Gujarati, MH Marathi, DL Hinglish. The tag is removed
from what the model sees so it is never echoed. Add a state by adding a REGION_STYLES entry.
"""
import re

from app.schemas import ChatMessage

REGION_STYLES = {
    "GJ": (
        "Gujarati written in English letters (Roman script), the way Gujaratis text on WhatsApp",
        "Use respectful 'tame', 'chhe/chho', 'tamne', 'tamaru' (not 'tu'). Examples of the feel: "
        "'Namaste! Tame bungee ke rafting, shu try karva mango chho?', 'Tame ketla loko chho?', "
        "'Tame kyare jaava mango chho?', 'Hu check kari ne kahu chhu', 'Price 3,500 chhe, already discounted chhe'.",
    ),
    "MH": (
        "Marathi written in English letters (Roman script), the way Maharashtrians text on WhatsApp",
        "Use respectful 'tumhi', 'aahe/aahat', 'tumhala'. For yourself use feminine endings ('mi check karte', "
        "'mi sangte', 'mi pathavte'). Examples of the feel: 'Namaskar! Tumhi bungee ki rafting, kashacha plan aahe?', "
        "'Tumhi kiti jan aahat?', 'Tumhala konti date pahije?', 'Price 3,500 aahe, already discounted aahe'.",
    ),
    "DL": (
        "Hinglish (Hindi in English letters), the way people in Delhi text on WhatsApp",
        "Use respectful 'aap'. For yourself use feminine forms ('check karti hoon', 'bata deti hoon'). Examples of "
        "the feel: 'Hi! Bungee ke liye 85m ya 111m, kaunsa dekhna hai?', 'Kitne log honge?', "
        "'Main check karke batati hoon'.",
    ),
}

_TAG_RE = re.compile(r"\s*[(\[]\s*(" + "|".join(REGION_STYLES) + r")\s*[)\]]", re.IGNORECASE)


def split_region_tag(text: str) -> tuple[str, str | None]:
    """(text without the state tag, the code or None). The code is upper-case."""
    m = _TAG_RE.search(text)
    if not m:
        return text, None
    return _TAG_RE.sub("", text).strip(), m.group(1).upper()


def region_from_messages(messages: list[ChatMessage]) -> tuple[list[ChatMessage], str | None]:
    """Region from the FIRST user message, plus the messages with that tag removed (copies)."""
    first = next((i for i, m in enumerate(messages) if m.role == "user"), None)
    if first is None:
        return messages, None
    clean, code = split_region_tag(messages[first].content)
    if not code:
        return messages, None
    cleaned = list(messages)
    cleaned[first] = messages[first].model_copy(update={"content": clean or "Hi"})
    return cleaned, code


def region_prompt(code: str) -> str:
    language, detail = REGION_STYLES[code]
    return (
        "## Reply language — set by the customer's region, OVERRIDES the 'mirror their language' rule\n"
        f"Reply in {language}, from your very first reply and for the whole chat, even if the customer writes "
        "in plain English. Switch only if they clearly ask for another language.\n"
        "- Roman script only. Never use Devanagari or Gujarati script.\n"
        f"- {detail}\n"
        "- Keep activity names, prices, numbers, dates, times and links exactly as the tools give them.\n"
        "- If you are not sure of a word, use the simple English word instead; mixing English in is natural. "
        "Never guess a Gujarati/Marathi word.\n"
        "- Never mention the customer's state, region or any tag, and do not claim to be from there.\n"
        "- Everything else (short messages, honesty rules, handoff, no discount promises) stays as above."
    )
