"""Client for bucketlistt's live MCP server — catalog + auth + cart tier.

Whitelists browse tools (destinations, experiences, activities, slots, add-ons),
OTP-based auth (send_otp, verify_otp), and cart management (add_to_cart, get_cart,
update_cart_item, remove_from_cart), plus read-only get_my_bookings.

**create_booking_order stays excluded.** The bot never charges anyone: the one
payment tool it has is create_payment_link, which only returns a Razorpay link the
customer pays on themselves. It is whitelisted here but offered to the model ONLY on
WhatsApp for a logged-in session (gated per turn in app/llm.py, like the bungee tool),
so the web chat's tool surface is unchanged.

The authToken from verify_otp flows through the conversation naturally — the
LLM sees it in the tool result and passes it into subsequent authenticated
tool calls. Each user's token is scoped to their conversation.
"""
import asyncio
import json
import logging
import re
import time
import httpx
from contextlib import AsyncExitStack

import litellm.experimental_mcp_client as litellm_mcp
from mcp import ClientSession
try:
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:
    from mcp.client.streamable_http import streamable_http_client as streamablehttp_client

from app import cache as mcp_cache
from app.config import settings

logger = logging.getLogger(__name__)


class _DotDict(dict):
    """Dict that also supports attribute access — litellm expects both
    (call_catalog_tool uses .function.name, its MCP transform uses ["function"])."""
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__

# Tools safe to cache in Redis (see app/cache.py), shared across every user.
# Deliberately excludes:
#   - get_time_slots / get_activity_slots: live availability, changes as
#     other users book — a 2h-stale "yes" could send someone to book a slot
#     that's already gone.
#   - send_otp / verify_otp / add_to_cart / get_cart / update_cart_item /
#     remove_from_cart / get_my_bookings: auth or per-user cart/booking
#     state — caching these would leak one user's data to another.
CACHEABLE_TOOLS = {
    "get_destinations",
    "get_experiences",
    "get_experience",
    "get_activities",
    "get_activities_summary",
    "get_activity",
    "search_activities_by_destination_and_tag",
    "get_activity_addons",
}

# Compact bungee-only discovery tool — gated in app/llm.py so it's only ever
# offered to the LLM when the user's message is about bungee jumping.
BUNGEE_SUMMARY_TOOL = "get_activities_summary"

# Returns a payment link for the customer's cart; gated in app/llm.py (WhatsApp + logged in).
PAYMENT_TOOL = "create_payment_link"

ALLOWED_TOOLS = {
    # Browse (read-only, no auth)
    "get_destinations",
    "get_experiences",
    "get_experience",
    "get_activities",
    # Compact discovery shape (no media/location) — bungee jumping only, see
    # BUNGEE_SUMMARY_TOOL gating in app/llm.py. Falls back to get_activities /
    # get_activity when the user needs the fields this tool leaves out.
    "get_activities_summary",
    "get_activity",
    "search_activities_by_destination_and_tag",
    "get_activity_slots",
    "get_time_slots",  # auth-free twin of get_activity_slots; preferred for timings
    "get_activity_addons",
    # Auth (SMS OTP login only)
    "send_otp",
    "verify_otp",
    # Cart (build up an order, no payment)
    "add_to_cart",
    "get_cart",
    "update_cart_item",
    "remove_from_cart",
    # Pay-by-link (WhatsApp, logged-in only — see PAYMENT_TOOL)
    "create_payment_link",
    # Post-book read
    "get_my_bookings",
}



# Shared, keep-alive HTTP transport — reused across every MCP call so the
# TCP/TLS handshake isn't redone per call. Each call still creates its own
# fresh ClientSession (below) — only the transport connection is pooled,
# never the session itself.
# To prevent the MCP SDK from closing the shared client after a single request,
# we wrap it in NoCloseWrapper below when passing it to httpx_client_factory.
_http_client: httpx.AsyncClient | None = None

class _NoCloseWrapper:
    """Wraps an httpx.AsyncClient to intercept and no-op __aexit__.
    
    The mcp SDK (1.28.1) takes an httpx_client_factory and internally does:
      async with factory() as client:
    Since we want to share the client, we cannot let the SDK close it.
    This wrapper delegates all attributes to the real client but prevents closure.
    """
    def __init__(self, client: httpx.AsyncClient):
        self._client = client
        
    def __getattr__(self, name):
        return getattr(self._client, name)
        
    async def __aenter__(self):
        # Return self so that methods called on the context manager (like .stream)
        # go through __getattr__ to the real client.
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient()
    return _http_client

def _client_factory(*args, **kwargs):
    # The mcp SDK passes headers, timeout, and auth to the factory.
    # We are sharing a single client, so we ignore per-factory instantiation args.
    # This is safe here because we only connect to a single fixed MCP server
    # without dynamic auth headers per session.
    return _NoCloseWrapper(_get_http_client())


async def close_http_client() -> None:
    """Call from the app lifespan shutdown so the pooled connections close cleanly."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


_MCP_CONNECT_TIMEOUT_SECONDS = 30


async def _fresh_session():
    """Create a fresh MCP session. Returns (stack, session).

    Caller MUST call stack.aclose() when done — never hold the session across
    an async generator yield, or anyio cancel scopes will leak into Starlette's
    task groups and crash the streaming response.

    The session itself is always freshly created per call (safe under this
    app's parallel-tool-call fan-out); only the underlying HTTP transport
    connection is reused, via the shared client below.

    Bounded by _MCP_CONNECT_TIMEOUT_SECONDS: without this, a wedged/misrouted
    MCP server (e.g. DNS pointing at the wrong host) hangs session.initialize()
    forever — the request never times out on its own, and an external
    cancellation lands mid-handshake and corrupts anyio's cancel scopes,
    crashing the whole streaming response instead of failing cleanly. A plain
    TimeoutError here is caught like any other error by stream_chat_response's
    except block, so the user gets a friendly retry message instead of a hang.
    """
    stack = AsyncExitStack()
    await stack.__aenter__()
    try:
        # We pass the no-close wrapper factory to streamablehttp_client
        read, write, _ = await stack.enter_async_context(
            streamablehttp_client(settings.mcp_server_url, httpx_client_factory=_client_factory)
        )
        session = await stack.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(session.initialize(), timeout=_MCP_CONNECT_TIMEOUT_SECONDS)
    except BaseException:
        await stack.aclose()
        raise
    return stack, session


# -- Tool schema cache (schemas don't change at runtime) ---------------------
_cached_tools: list[dict] = []
_tools_cached_at: float = 0.0
_TOOLS_CACHE_TTL = 300  # 5 minutes


async def load_catalog_tools() -> list[dict]:
    """OpenAI-format schemas for the whitelisted catalog tools.

    Cached in-memory for 5 minutes — avoids a network round-trip to the MCP
    server on every single user message.
    """
    global _cached_tools, _tools_cached_at
    if not settings.mcp_server_url:
        return []
    now = time.monotonic()
    if _cached_tools and (now - _tools_cached_at) < _TOOLS_CACHE_TTL:
        logger.debug("MCP tool schemas served from cache (%d tools)", len(_cached_tools))
        return _cached_tools
    t0 = time.perf_counter()
    stack, session = await _fresh_session()
    try:
        tools = await litellm_mcp.load_mcp_tools(session, format="openai")
        _cached_tools = [t for t in tools if t["function"]["name"] in ALLOWED_TOOLS]
        _tools_cached_at = now
        logger.info("Loaded %d MCP tool schemas (of %d total) in %.3fs", len(_cached_tools), len(tools), time.perf_counter() - t0)
    finally:
        try:
            await stack.aclose()
        except Exception:
            pass
    return _cached_tools


# Hard ceiling per tool result — only hit after field-stripping, as a last resort.
MAX_TOOL_RESULT_CHARS = 16000

_DROP_KEYS = {
    "media", "images", "primaryMedia", "logo", "image", "__v", "clientId",
    "createdAt", "updatedAt", "uniqueCode", "advancePercentage", "highlightedOrder",
    "order", "forAgent", "isHighlighted", "isApproved", "category",
    "timeSlots",
    "bucketlisttSeasonalClosures",
}
_HTML_KEYS = {"description", "highlights", "inclusion", "exclusion", "subtitle", "eligibility"}
_HTML_RE = re.compile(r"<[^<>]+>")   # not [^>]+: a long run of "<" with no ">" was quadratic
# Longer limit for fields that carry critical differentiating info (inclusions, descriptions).
_HTML_TRUNC_LONG = 800
_HTML_TRUNC_SHORT = 300
_LONG_HTML_KEYS = {"description", "inclusion", "exclusion", "highlights"}

_SEARCH_KEEP = {"_id", "title", "actualPrice", "discountedPrice", "subtitle"}



import datetime

def _active_closure(closures: list, on_date: str) -> dict | None:
    """Return the closure record covering `on_date` (YYYY-MM-DD), if any."""
    for c in closures or []:
        if c.get("isActive") and c.get("startDate", "") <= on_date <= c.get("endDate", ""):
            return c
    return None

def _slim(obj):
    """Recursively drop media/HTML bulk from an MCP JSON result."""
    if isinstance(obj, dict):
        out = {}
        if "bucketlisttSeasonalClosures" in obj:
            today = datetime.date.today().isoformat()
            closure = _active_closure(obj["bucketlisttSeasonalClosures"], today)
            if closure:
                out["_closed_until"] = closure.get("endDate")
                out["_closure_reason"] = closure.get("message")

        for k, v in obj.items():
            if k in _DROP_KEYS:
                continue
            if k in _HTML_KEYS and isinstance(v, str):
                limit = _HTML_TRUNC_LONG if k in _LONG_HTML_KEYS else _HTML_TRUNC_SHORT
                out[k] = _HTML_RE.sub("", v).strip()[:limit]
            else:
                out[k] = _slim(v)
        return out
    if isinstance(obj, list):
        return [_slim(x) for x in obj]
    return obj


def _compact_search(raw):
    """Reduce a tag-search result to provider names + activity summaries.

    Keeps subtitle and a short description snippet so the LLM can
    differentiate activities (e.g. Dronecraft's drone+DSLR coverage vs plain
    rafting) without needing a follow-up get_activity call.
    """
    if not isinstance(raw, dict) or "data" not in raw:
        return _slim(raw)
    out = {k: v for k, v in raw.items() if k != "data"}
    out["data"] = []
    for group in raw.get("data", []):
        if not isinstance(group, dict):
            out["data"].append(group)
            continue
        activities = []
        for act in group.get("activities", []):
            if not isinstance(act, dict):
                continue
            item = {k: v for k, v in act.items() if k in _SEARCH_KEEP}
            # Keep a description snippet so the LLM sees what makes each
            # activity unique (drone coverage, included perks, etc.)
            desc = act.get("description", "")
            if isinstance(desc, str) and desc:
                item["description"] = _HTML_RE.sub("", desc).strip()[:200]
            inclusion = act.get("inclusion", "")
            if isinstance(inclusion, str) and inclusion:
                item["inclusion"] = _HTML_RE.sub("", inclusion).strip()[:200]
            activities.append(item)
        compact = {
            "experience": group.get("experience"),
            "experienceId": group.get("experienceId"),
            "activities": activities,
        }
        out["data"].append(compact)
    return out


async def _postprocess(fn: str, text: str, tool_args: dict, session=None) -> dict:
    """Slim, truncate, and add hints to a tool result."""
    raw_len = len(text)
    try:
        parsed = json.loads(text)
        if fn == "search_activities_by_destination_and_tag":
            text = json.dumps(_compact_search(parsed), separators=(",", ":"))
        else:
            text = json.dumps(_slim(parsed), separators=(",", ":"))
    except (ValueError, TypeError):
        pass
    slimmed_len = len(text)
    # BUNGEE_SUMMARY_TOOL is exempt: it's the tool that exists specifically so
    # bungee queries DON'T get cut off mid-JSON like the old large-dump tool
    # did. Its own compact shape + gating to bungee-only queries keeps it
    # bounded (~25-30k chars for a full multi-provider Rishikesh bungee
    # listing, measured against the live server) — well within context budget.
    truncated = fn != BUNGEE_SUMMARY_TOOL and slimmed_len > MAX_TOOL_RESULT_CHARS
    if truncated:
        text = text[:MAX_TOOL_RESULT_CHARS] + "\n...[truncated; use a more specific query or `select`]"
    logger.info("MCP result %s: %d raw, %d slimmed%s", fn, raw_len, slimmed_len, f", TRUNCATED at {MAX_TOOL_RESULT_CHARS}" if truncated else "")
    result = {"result": text}
    if fn == "search_activities_by_destination_and_tag":
        result["_hint"] = (
            "These are activity listings, NOT availability. To check if an activity "
            "is available on a specific date, call get_time_slots(activityId, date)."
        )
    if fn in ("get_time_slots", "get_activity_slots"):
        if '"slots":[]' in text.replace(" ", ""):
            activity_id = tool_args.get("activityId") or tool_args.get("identifier")
            date_req = tool_args.get("date")
            is_closed = False
            closure_reason = ""
            closed_until = ""
            
            if activity_id and date_req:
                try:
                    # Supplementary lookup to check for closure — goes through
                    # get_activity_by_id so caching/slimming apply the same as
                    # any other catalog call.
                    act_result = await get_activity_by_id(activity_id, session=session)

                    res_obj = json.loads(act_result.get("result") or "{}")
                    # get_activity's shape is {"success": bool, "data": {...}} —
                    # _slim() injects _closed_until into the nested "data" dict
                    # (where bucketlisttSeasonalClosures actually lives), not at
                    # the top level, so it must be read from there.
                    activity_data = res_obj.get("data") if isinstance(res_obj, dict) else None
                    if isinstance(activity_data, dict) and "_closed_until" in activity_data:
                        is_closed = True
                        closed_until = activity_data["_closed_until"]
                        closure_reason = activity_data["_closure_reason"]
                except Exception as e:
                    logger.error(f"Supplementary closure lookup failed: {e}")

            if is_closed:
                result["_hint"] = (
                    f"Zero slots for THIS activity because it is CLOSED for the season until {closed_until}. "
                    f"Reason: {closure_reason}. "
                    "Do NOT suggest same-category alternatives (like another rafting route) unless you "
                    "already know for sure they are open, because the entire category is likely closed."
                )
            else:
                result["_hint"] = (
                    "Zero slots for THIS activity. Other providers may offer the same "
                    "activity type with available slots — call search_activities_by_destination_and_tag "
                    "to find alternatives before telling the user it's unavailable."
                )
        else:
            result["_hint"] = (
                "Show ONLY the slot start time (e.g. '10:00 AM'). Do NOT show or "
                "fabricate an end time — the data does not have meaningful end times."
            )
    if fn == BUNGEE_SUMMARY_TOOL:
        logger.info("[bungee-summary] %s returned %d chars (truncated=%s)", fn, slimmed_len, truncated)
        result["_hint"] = (
            "This is a compact bungee-only summary (no location/media/full description). "
            "If the user asks for MORE detail than this has, call get_activity(identifier=...) "
            "on the specific activity's _id for the full record."
        )
    if fn == PAYMENT_TOOL:
        result["_hint"] = (
            "Send the customer the payment URL from this result EXACTLY as returned, as a bare URL on its own "
            "line with the amount. Do not edit, shorten or invent a link. Do not say the payment is done: the "
            "customer pays on that page and bucketlistt confirms it. If there is no URL in the result, say you "
            "could not create it and give the website cart link instead."
        )
    if fn == "add_to_cart":
        result["_hint"] = (
            "Item added. If you haven't suggested an add-on or combo yet in "
            "this conversation, consider calling get_activity_addons for the "
            "activity to check if there's a relevant add-on to mention (ONE "
            "only, and only if you haven't upsold already)."
        )
    # Dronecraft perks — injected directly into the tool result so the LLM
    # can't miss them. The catalog's inclusion field is trimmed and unreliable;
    # the LLM was hallucinating "lounge access" and dropping the real perks.
    if "dronecraft" in text.lower() or "drone craft" in text.lower():
        result["_dronecraft_perks"] = (
            "IMPORTANT — Every Dronecraft booking includes ALL of these confirmed perks. "
            "Mention them when presenting Dronecraft options:\n"
            "1. Drone + DSLR cinematic video coverage with edited Instagram reel\n"
            "2. Complimentary ₹500 voucher + reel\n"
            "3. Welcome drink\n"
            "4. Clothes/wetsuits\n"
            "5. Crocs\n"
            "6. Sunscreen\n"
            "7. Pickup & drop from/to starting pickup point\n"
            "Do NOT mention 'lounge access' or 'lockers' for Dronecraft — "
            "these are NOT Dronecraft perks. Use THIS list, not the inclusion field."
        )
    return result


async def get_activity_by_id(identifier: str, session=None) -> dict:
    """Fetch one activity's full (slimmed) record by id, outside the LLM tool loop.

    Reuses call_catalog_tool so caching/slimming/truncation all apply — this is
    just a direct-call shortcut for callers (e.g. a REST endpoint) that already
    know which activity they want.
    """
    dummy_call = _DotDict(
        function=_DotDict(name="get_activity", arguments=json.dumps({"identifier": identifier}))
    )
    return await call_catalog_tool(dummy_call, session=session)


async def call_catalog_tool(tool_call, session=None) -> dict:
    """Execute one whitelisted catalog tool call against the live MCP server.

    If session is provided, reuses the existing session. Otherwise creates a
    fresh session per call so anyio cancel scopes are fully contained
    and never leak into the caller's task.
    """
    fn = tool_call.function.name
    if fn == BUNGEE_SUMMARY_TOOL:
        logger.info("[bungee-summary] %s invoked args=%s", fn, tool_call.function.arguments)

    cache_k = None
    if fn in CACHEABLE_TOOLS:
        cache_k = mcp_cache.cache_key(fn, tool_call.function.arguments or "{}")
        cached = await mcp_cache.get(cache_k)
        if cached is not None:
            logger.info("MCP cache hit %s", fn)
            return json.loads(cached)

    t0 = time.perf_counter()
    if session is None:
        stack, active_session = await _fresh_session()
        owns_session = True
    else:
        active_session = session
        owns_session = False

    try:
        result = await litellm_mcp.call_openai_tool(session=active_session, openai_tool=tool_call)
    except Exception as exc:
        logger.exception("MCP call %s failed after %.3fs", fn, time.perf_counter() - t0)
        from app.notifier import send_critical_alert
        asyncio.create_task(send_critical_alert("mcp_tool_error", str(exc), f"Failed to execute MCP tool: {fn}"))
        raise
    finally:
        if owns_session:
            try:
                await stack.aclose()
            except Exception:
                pass
    logger.info("MCP call %s completed in %.3fs", fn, time.perf_counter() - t0)
    text = "\n".join(part.text for part in result.content if hasattr(part, "text"))
    text = text or str(result)
    postprocessed = await _postprocess(fn, text, json.loads(tool_call.function.arguments or "{}"), session=active_session)

    if cache_k:
        await mcp_cache.set(cache_k, json.dumps(postprocessed, separators=(",", ":")))

    return postprocessed
