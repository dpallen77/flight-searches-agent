"""Agent layer. The UI only knows the event contract returned by
handle_message() and handle_approval().

Events: {"type":"text"}, {"type":"approval"}, {"type":"offers"}

Modes (chosen automatically):
  - Claude agent: ANTHROPIC_API_KEY set and `anthropic` installed
  - Regex fallback: otherwise (format: "SEA to LIS on 2026-11-10 flexible")
Fares: Ignav when IGNAV_API_KEY is set, otherwise mock data.
The spending gate (confirm above 5 calls, hard caps, budget) is plain code,
so the model can't bypass it.
"""
import hashlib
import json
import logging
import os
import random
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

CONFIRM_ABOVE_CALLS = 5      # ask before spending more than this many new calls
MAX_CALLS_PER_REQUEST = 25   # hard cap, even if approved
MAX_TOOL_ROUNDS = 8          # max model<->tool round trips per user message
MONTHLY_LIMIT = 1000         # Ignav free tier
CACHE_TTL_S = 6 * 3600
IGNAV_URL = "https://ignav.com/api/fares/one-way"
USAGE_FILE = Path(__file__).with_name("usage.json")
log = logging.getLogger("flight-agent")


class ProviderError(Exception):
    pass


# ---------- persistent call counter (survives restarts, resets monthly) ----------
def _load_usage():
    month = time.strftime("%Y-%m")
    try:
        data = json.loads(USAGE_FILE.read_text())
        if data.get("month") == month:
            return data
    except (OSError, ValueError):
        pass
    return {"month": month, "used": 0}


usage = _load_usage()
cache = {}          # (origin, dest, day, filters_json) -> (timestamp, offers)
pending = {}        # session_id -> {"plan", "tool_use_id", "new"} awaiting approval
conversations = {}  # session_id -> Claude message history


def _count_call():
    usage["used"] += 1
    try:
        USAGE_FILE.write_text(json.dumps(usage))
    except OSError:
        pass


def live_mode():
    return bool(os.environ.get("IGNAV_API_KEY"))


# ---------- provider adapters ----------
def mock_search(origin, dest, day, filters):
    _count_call()
    rng = random.Random(int(hashlib.md5(f"{origin}{dest}{day}".encode()).hexdigest(), 16))
    carriers = ["Delta", "Icelandair", "TAP Air Portugal", "Lufthansa", "United"]
    offers = []
    for _ in range(3):
        stops = rng.choice([0, 1, 1, 2])
        offers.append({"date": day, "carrier": rng.choice(carriers), "currency": "USD",
                       "price": rng.randint(520, 1100) - stops * 60, "stops": stops,
                       "minutes": 600 + stops * 180 + rng.randint(0, 120), "id": None})
    if "max_stops" in filters:
        offers = [o for o in offers if o["stops"] <= filters["max_stops"]]
    if "max_price" in filters:
        offers = [o for o in offers if o["price"] <= filters["max_price"]]
    return offers


def ignav_search(origin, dest, day, filters):
    payload = {"origin": origin, "destination": dest, "departure_date": day, **filters}
    req = urllib.request.Request(IGNAV_URL, data=json.dumps(payload).encode(), method="POST", headers={
        "X-Api-Key": os.environ["IGNAV_API_KEY"], "Content-Type": "application/json"})
    try:
        try:
            import certifi  # optional: fixes certificate errors on some Python installs
            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ctx = None
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        log.error("Ignav HTTP %s for %s->%s %s: %s", e.code, origin, dest, day, detail)
        hints = {401: "API key rejected. Check IGNAV_API_KEY in .env.",
                 429: "Rate limited. Wait a moment and retry."}
        raise ProviderError(hints.get(e.code, f"Ignav returned HTTP {e.code}: {detail[:200]}"))
    except urllib.error.URLError as e:
        log.exception("Ignav connection failed for %s->%s %s", origin, dest, day)
        if isinstance(e.reason, ssl.SSLCertVerificationError):
            raise ProviderError("SSL certificate check failed. Run: python3 -m pip install certifi")
        raise ProviderError(f"Couldn't reach Ignav ({type(e.reason).__name__}: {e.reason}).")
    except TimeoutError:
        log.exception("Ignav timed out")
        raise ProviderError("Ignav took longer than 30 seconds to respond.")
    _count_call()  # failed requests aren't billed, so only count successes
    offers = []
    for it in data.get("itineraries", []):
        out = it["outbound"]
        segs = out.get("segments", [])
        carrier = out.get("carrier") or (segs[0].get("operating_carrier_name") if segs else None) or "Unknown"
        offers.append({"date": day, "carrier": carrier, "currency": it["price"].get("currency", "USD"),
                       "price": it["price"]["amount"], "stops": max(len(segs) - 1, 0),
                       "minutes": out.get("duration_minutes") or 0, "id": it.get("ignav_id")})
    return offers


def provider_search(origin, dest, day, fjson):
    filters = json.loads(fjson)
    return ignav_search(origin, dest, day, filters) if live_mode() else mock_search(origin, dest, day, filters)


# ---------- planning, gate, execution (plain code, not the model) ----------
def plan_items(plan):
    start = date.fromisoformat(plan["date"])
    fjson = json.dumps(plan["filters"], sort_keys=True)
    return [(plan["origin"], plan["dest"], (start + timedelta(days=i)).isoformat(), fjson)
            for i in range(-plan["flex"], plan["flex"] + 1)]


def is_fresh(key):
    hit = cache.get(key)
    return bool(hit and time.time() - hit[0] < CACHE_TTL_S)


def assess(plan):
    """Returns (status, new_calls, items, message). status: ok | approve | error."""
    items = plan_items(plan)
    new = sum(1 for k in items if not is_fresh(k))
    remaining = MONTHLY_LIMIT - usage["used"]
    if new > MAX_CALLS_PER_REQUEST:
        return "error", new, items, f"That needs {new} new calls, over the {MAX_CALLS_PER_REQUEST}-call limit per request. Use fewer flexible days."
    if new > remaining:
        return "error", new, items, f"That needs {new} calls but only {remaining} remain this month."
    if new > CONFIRM_ABOVE_CALLS:
        return "approve", new, items, None
    return "ok", new, items, None


def approval_event(plan, new, items):
    remaining = MONTHLY_LIMIT - usage["used"]
    route = f'{plan["origin"]} to {plan["dest"]}, {plan["date"]}' + (f' ±{plan["flex"]} days' if plan["flex"] else "")
    return {"type": "approval", "new_calls": new, "cached": len(items) - new, "used": usage["used"],
            "limit": MONTHLY_LIMIT, "after": remaining - new, "route": route}


def collect(plan):
    before = usage["used"]
    results, error = [], None
    for key in plan_items(plan):
        if not is_fresh(key):
            try:
                cache[key] = (time.time(), provider_search(*key))
            except ProviderError as e:
                error = str(e)
                break  # stop spending calls after a failure
        results.extend(cache[key][1])
    results.sort(key=lambda o: o["price"])
    return results, usage["used"] - before, error


# ---------- Claude tool-use loop ----------
TOOLS = [{
    "name": "search_flights",
    "description": ("Search live one-way fares. Each departure date not already cached costs one paid API call, "
                    "so keep searches minimal. flexible_days=N also searches N days either side (2N+1 calls). "
                    "Results are sorted by price."),
    "input_schema": {"type": "object", "properties": {
        "origin": {"type": "string", "description": "3-letter IATA airport or metro code, e.g. SEA or LON"},
        "destination": {"type": "string", "description": "3-letter IATA airport or metro code"},
        "departure_date": {"type": "string", "description": "YYYY-MM-DD"},
        "flexible_days": {"type": "integer", "minimum": 0, "maximum": 7,
                          "description": "Also search this many days before and after. Default 0."},
        "max_stops": {"type": "integer", "minimum": 0, "maximum": 2},
        "cabin_class": {"type": "string", "enum": ["economy", "premium_economy", "business", "first"]},
        "max_price": {"type": "integer", "description": "Strict maximum price"}},
        "required": ["origin", "destination", "departure_date"]},
}]


def system_prompt():
    return (f"You are a personal flight-price assistant. Today is {date.today().isoformat()}.\n"
            "- Use search_flights for fares; never invent prices. Convert city names to IATA or metro codes yourself.\n"
            "- Every search spends a limited paid-call budget. Prefer the smallest search that answers the question, "
            "and use flexible_days only when the user is flexible. A request needing more than "
            f"{CONFIRM_ABOVE_CALLS} new calls will pause for the user's approval automatically.\n"
            "- Only one-way fares are supported. For round trips, search each direction separately and say so.\n"
            "- If the origin, destination, or date is missing or ambiguous, ask one short question first.\n"
            "- Compare on total trade-offs (price, stops, duration), name the best pick and why, and mention the currency. "
            "Fares change quickly, so remind the user to verify the price on the airline's site before booking.\n"
            "- Keep replies short. The UI already shows a table of the cheapest results, so don't repeat every row.")


_client = None


def claude_ready():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        log.warning("ANTHROPIC_API_KEY is set but the 'anthropic' package is missing: python3 -m pip install anthropic")
        return False


def plan_from_args(a):
    o, d = str(a.get("origin", "")).upper().strip(), str(a.get("destination", "")).upper().strip()
    if not (re.fullmatch(r"[A-Z]{3}", o) and re.fullmatch(r"[A-Z]{3}", d)):
        raise ValueError("origin and destination must be 3-letter IATA or metro codes")
    if date.fromisoformat(a["departure_date"]) < date.today():
        raise ValueError("departure_date is in the past")
    filters = {}
    if a.get("max_stops") is not None:
        filters["max_stops"] = int(a["max_stops"])
    if a.get("cabin_class"):
        filters["cabin_class"] = str(a["cabin_class"])
    if a.get("max_price"):
        filters["max_price"] = int(a["max_price"])
    flex = max(0, min(7, int(a.get("flexible_days") or 0)))
    return {"origin": o, "dest": d, "date": a["departure_date"], "flex": flex, "filters": filters}


def do_search(plan, events):
    results, made, error = collect(plan)
    if results:
        events.append({"type": "offers", "items": results[:8]})
    keep = ("date", "carrier", "price", "currency", "stops", "minutes")
    return json.dumps({"calls_used": made, "error": error,
                       "note": None if results else "No fares found for these dates and filters.",
                       "offers": [{k: o[k] for k in keep} for o in results[:10]]})


def run_tool(sid, tu, events):
    """Returns the tool_result string, or None if paused for user approval."""
    log.info("tool %s %s", tu.name, tu.input)
    if tu.name != "search_flights":
        return f"Unknown tool {tu.name}"
    try:
        plan = plan_from_args(tu.input)
    except (KeyError, ValueError, TypeError) as e:
        return f"Invalid arguments: {e}"
    status, new, items, msg = assess(plan)
    if status == "error":
        return msg
    if status == "approve":
        pending[sid] = {"plan": plan, "tool_use_id": tu.id, "new": new}
        events.append(approval_event(plan, new, items))
        return None
    return do_search(plan, events)


def run_loop(sid, events):
    import anthropic
    global _client
    _client = _client or anthropic.Anthropic()
    msgs = conversations[sid]
    for _ in range(MAX_TOOL_ROUNDS):
        try:
            resp = _client.messages.create(
                model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"), max_tokens=1024,
                system=system_prompt(), tools=TOOLS, messages=msgs,
                tool_choice={"type": "auto", "disable_parallel_tool_use": True})
        except Exception as e:
            log.exception("Claude API call failed")
            events.append({"type": "text", "text": f"Claude API error: {type(e).__name__}: {e}"})
            return events
        msgs.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            events.append({"type": "text", "text": text or "(no reply)"})
            return events
        tu = next(b for b in resp.content if b.type == "tool_use")
        out = run_tool(sid, tu, events)
        if out is None:
            return events  # paused: waiting for approval
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tu.id, "content": out}]})
    msgs.append({"role": "assistant", "content": "(stopped: too many search steps)"})
    events.append({"type": "text", "text": "I stopped after too many search steps. Try a narrower request."})
    return events


# ---------- regex fallback (no Claude key) ----------
def parse_request(text):
    m = re.search(r"\b([A-Za-z]{3})\s*(?:to|->|→|-)\s*([A-Za-z]{3})\b", text)
    d = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if not (m and d):
        return None
    f = re.search(r"±\s*(\d+)", text)
    flex = int(f.group(1)) if f else (3 if "flexible" in text.lower() else 0)
    return {"origin": m.group(1).upper(), "dest": m.group(2).upper(), "date": d.group(0),
            "flex": min(flex, 7), "filters": {}}


def legacy_execute(plan):
    results, made, error = collect(plan)
    events = []
    if error:
        events.append({"type": "text", "text": f"Search stopped: {error} ({made} call{'s' if made != 1 else ''} used.)"})
    if results:
        if not error:
            events.append({"type": "text", "text": f'Searched {plan["origin"]} to {plan["dest"]}. Used {made} new API call{"s" if made != 1 else ""}; the rest came from cache. Cheapest options:'})
        events.append({"type": "offers", "items": results[:8]})
    elif not error:
        events.append({"type": "text", "text": "No fares found for those dates."})
    return events


def legacy_message(sid, text):
    plan = parse_request(text)
    if not plan:
        return [{"type": "text", "text": "Give me a route and date, for example: SEA to LIS on 2026-11-10, or add 'flexible' or '±3'. (Add ANTHROPIC_API_KEY to .env for plain-language requests.)"}]
    try:
        status, new, items, msg = assess(plan)
    except ValueError:
        return [{"type": "text", "text": "That date isn't valid. Use YYYY-MM-DD."}]
    if status == "error":
        return [{"type": "text", "text": msg}]
    if status == "approve":
        pending[sid] = {"plan": plan, "tool_use_id": None, "new": new}
        return [approval_event(plan, new, items)]
    return legacy_execute(plan)


# ---------- public API used by the server ----------
def handle_message(sid, text):
    if not claude_ready():
        return legacy_message(sid, text)
    content = []
    p = pending.pop(sid, None)
    if p and p["tool_use_id"]:  # unresolved approval: close it out before the new message
        content.append({"type": "tool_result", "tool_use_id": p["tool_use_id"],
                        "content": "The user sent a new message instead of approving. Nothing was searched."})
    content.append({"type": "text", "text": text})
    conversations.setdefault(sid, []).append({"role": "user", "content": content})
    return run_loop(sid, [])


def handle_approval(sid, decision):
    p = pending.pop(sid, None)
    if not p:
        return [{"type": "text", "text": "Nothing is waiting for approval."}]
    if p["tool_use_id"] is None:  # regex-fallback flow
        if decision == "proceed":
            return legacy_execute(p["plan"])
        if decision == "narrow":
            p["plan"]["flex"] = min(p["plan"]["flex"], 1)
            status, new, items, msg = assess(p["plan"])
            return legacy_execute(p["plan"]) if status == "ok" else [{"type": "text", "text": msg or "Still too large."}]
        return [{"type": "text", "text": "Cancelled. No API calls were made."}]
    events = []
    if decision == "proceed":
        out = do_search(p["plan"], events)
    elif decision == "narrow":
        out = json.dumps({"declined_by_user": True, "reason": (
            f"The user wants a cheaper search; that plan needed {p['new']} new calls. Run a narrower version using "
            f"at most {CONFIRM_ABOVE_CALLS} new calls (fewer flexible days or fewer options), or ask what to prioritize.")})
    else:
        out = "The user cancelled. No calls were made. Acknowledge briefly and offer alternatives."
    conversations[sid].append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": p["tool_use_id"], "content": out}]})
    return run_loop(sid, events)


def usage_snapshot():
    return {"used": usage["used"], "limit": MONTHLY_LIMIT,
            "mode": "live" if live_mode() else "mock", "agent": "claude" if claude_ready() else "basic"}