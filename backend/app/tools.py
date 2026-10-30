"""Local tools exposed to the LLM via function/tool calling.

- `escalate_and_capture_lead`: Capture high-value group booking leads or human agent requests,
  saving them to backend/data/leads.json and generating an escalation ticket reference.
- `search_web`: Fresh web-search fallback via Claude's web_search server-side tool. Used only
  when the KB and MCP catalog don't have an answer (e.g. current season status, weather,
  time-sensitive info). The system prompt tells the model to reach for it sparingly.

Live catalog/availability data (destinations, activities, real-time slots) comes from the
bucketlistt MCP server instead — see app/mcp_client.py. That client is deliberately
restricted to read-only tools; identity, cart, and payment tools are never loaded, so this
chatbot has no way to log a user in, touch a cart, or move money.
"""
import inspect
import json
import logging
import random
import time
from pathlib import Path

import anthropic

from app import session_store
from app.config import settings

logger = logging.getLogger(__name__)

LEADS_FILE = Path(__file__).parent.parent / "data" / "leads.json"


def _ensure_leads_file() -> None:
    """Ensure data/leads.json exists."""
    if not LEADS_FILE.exists():
        LEADS_FILE.parent.mkdir(parents=True, exist_ok=True)
        LEADS_FILE.write_text("[]", encoding="utf-8")


def escalate_and_capture_lead(
    name: str = "",
    phone: str = "",
    email: str = "",
    group_size: int = 1,
    activity_interest: str = "",
    preferred_date: str = "",
    notes: str = "",
    urgency: str = "normal",
) -> dict:
    """Capture a lead or human agent request and generate an escalation ticket."""
    _ensure_leads_file()

    ticket_id = f"LEAD-{random.randint(10000, 99999)}"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    lead_record = {
        "ticket_id": ticket_id,
        "timestamp": timestamp,
        "name": name or "Guest",
        "phone": phone,
        "email": email,
        "group_size": group_size,
        "activity_interest": activity_interest,
        "preferred_date": preferred_date,
        "notes": notes,
        "urgency": urgency,
        "status": "PENDING_HUMAN_CALLBACK",
    }

    try:
        data = json.loads(LEADS_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Could not read %s, starting fresh", LEADS_FILE)
        data = []

    data.append(lead_record)
    LEADS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("Lead captured: %s (urgency=%s)", ticket_id, urgency)

    return {
        "status": "success",
        "ticket_id": ticket_id,
        "message": (
            f"Escalation lead captured successfully. Ticket #{ticket_id} created. "
            f"Human team will contact {name or 'Guest'} at {phone or 'the provided contact'} shortly."
        ),
        "lead_details": lead_record,
    }


_AVAILABILITY_KEYWORDS = {"monsoon", "season", "closed", "open now", "rainy", "available", "availability"}


_aclient: anthropic.AsyncAnthropic | None = None


def get_aclient() -> anthropic.AsyncAnthropic:
    """Shared Anthropic client — also reused by app/dashboard.py for summaries."""
    global _aclient
    if _aclient is None:
        _aclient = anthropic.AsyncAnthropic()
    return _aclient


async def search_web(query: str) -> dict:
    """Use Claude's web_search server-side tool to answer a query with fresh web info."""
    lower = query.lower()
    if any(kw in lower for kw in _AVAILABILITY_KEYWORDS):
        logger.info("search_web blocked — availability keyword in query: %s", query[:100])
        return {
            "result": (
                "Do NOT use web search for availability or seasonal status. "
                "Call get_time_slots with the activityId and date — that is the "
                "only source of truth. If one provider has no slots, try others "
                "via search_activities_by_destination_and_tag."
            )
        }
    t0 = time.perf_counter()
    logger.info("search_web query: %s", query[:150])
    try:
        response = await get_aclient().messages.create(
            model=settings.web_search_model,
            max_tokens=1024,
            # web_search_20260209 (dynamic filtering) requires Opus 5/4.8/4.7/4.6,
            # Sonnet 5, or Sonnet 4.6 — WEB_SEARCH_MODEL defaults to claude-haiku-4-5
            # (deliberately cheap/fast, see config.py), which isn't on that list and
            # 400s with "does not support programmatic tool calling" on the newer
            # variant. The basic variant has no such model restriction and is the
            # correct choice for whatever model WEB_SEARCH_MODEL is actually set to.
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": query}],
        )
        text_parts = [b.text for b in response.content if hasattr(b, "text")]
        logger.info("search_web completed in %.3fs (%d text parts)", time.perf_counter() - t0, len(text_parts))
        return {"result": "\n".join(text_parts) or ""}
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("search_web failed after %.3fs", time.perf_counter() - t0)
        return {"error": f"web search failed: {exc}"}


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Search the live web when neither your knowledge base nor the Bucketlistt "
                "catalog tools have a SPECIFIC answer to the user's question. Use it for: "
                "(1) operator-specific safety facts the KB doesn't have — exact certification "
                "body, cord/equipment replacement schedule, whether insurance is included, "
                "what safety gear is provided, first-aid availability, rain/weather policy; "
                "(2) logistics details — parking at a specific site, phone policy during a jump, "
                "certificates issued, footwear advice; "
                "(3) policy details — cancellation / rescheduling / missed-slot rules not in the KB. "
                "NEVER use search_web for availability, season/monsoon status, whether an activity "
                "is open, prices, or activity listings — use `get_time_slots` and the catalog "
                "tools for those instead. The catalog tools are the ONLY source of truth for "
                "availability."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Focused search query, e.g. 'Rishikesh river rafting season status 2026'.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_and_capture_lead",
            "description": (
                "Capture high-value group booking leads (5+ people), bulk discount inquiries, "
                "custom itinerary requests, explicit customer requests to talk to a human agent, "
                "OR simply whenever the user volunteers their name/phone/email in conversation "
                "(e.g. after being asked to save their itinerary) — call it then too, with "
                "urgency='normal', even with no group booking involved. Generates an escalation "
                "ticket / contact record."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Customer's name if provided.",
                    },
                    "phone": {
                        "type": "string",
                        "description": "Customer's phone or WhatsApp number for human callback.",
                    },
                    "email": {
                        "type": "string",
                        "description": "Customer's email address if provided.",
                    },
                    "group_size": {
                        "type": "integer",
                        "description": "Number of people in the travel group (default 1).",
                    },
                    "activity_interest": {
                        "type": "string",
                        "description": "Activities or packages the group is interested in (e.g. '16km Rafting + Bungee for 12 people').",
                    },
                    "preferred_date": {
                        "type": "string",
                        "description": "Target date or month for the trip.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Summary of custom requirements, budget, or question for human team.",
                    },
                    "urgency": {
                        "type": "string",
                        "enum": ["normal", "medium", "high"],
                        "description": "Urgency level based on group size or explicit callback request.",
                    },
                },
                "required": [],
            },
        },
    },
]


_TOOL_REGISTRY = {
    "escalate_and_capture_lead": escalate_and_capture_lead,
    "search_web": search_web,
}


async def dispatch_tool(name: str, arguments: str, session_id: str | None = None) -> dict:
    """Execute a tool call by name, given its JSON-encoded arguments string."""
    func = _TOOL_REGISTRY.get(name)
    if func is None:
        logger.warning("dispatch_tool: unknown tool '%s'", name)
        return {"error": f"unknown tool: {name}"}
    kwargs = json.loads(arguments) if arguments else {}
    t0 = time.perf_counter()
    logger.info("dispatch_tool: %s(args=%s)", name, ", ".join(sorted(kwargs)))  # names only: values can be PII
    result = func(**kwargs)
    if inspect.isawaitable(result):
        result = await result
    logger.info("dispatch_tool: %s completed in %.3fs", name, time.perf_counter() - t0)

    # Unify both contact-capture paths: whenever a lead is captured via chat
    # (not just the old dedicated /api/session/user-info form), also save it
    # into the Redis session so it shows up in the dashboard summary below.
    if name == "escalate_and_capture_lead" and session_id and result.get("status") == "success":
        details = result.get("lead_details", {})
        if details.get("phone") or details.get("email"):
            await session_store.save_user_info(
                session_id, details.get("name", ""), details.get("phone", ""), details.get("email", "")
            )

    return result
