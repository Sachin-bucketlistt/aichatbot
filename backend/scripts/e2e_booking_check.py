"""End-to-end check of the v2 chat API against a running deployment.

Phase A (default, read-only): asks about EVERY activity type for next Saturday and checks each
reply is non-empty, error-free and contains a price. No login, nothing is added to a cart.

Phase B (--book): the real flow. You type your phone number and the OTP (never stored), the bot
logs you in and adds one of each activity to YOUR bucketlistt cart, shows the cart, then removes
everything again. Nothing is paid: the bot cannot take payment. Run it with a number you own.

    export BASE_URL=https://bucky-chat-test.onrender.com CHAT_API_KEY=...
    python scripts/e2e_booking_check.py            # phase A
    python scripts/e2e_booking_check.py --book     # phase A, then phase B
"""
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
KEY = os.environ["CHAT_API_KEY"]
ACTIVITIES = [
    "bungee jumping", "river rafting", "Dronecraft rafting", "paragliding", "zipline",
    "flying fox", "giant swing", "camping", "hot air balloon", "paramotoring",
    "aerial yoga", "Ganga Aarti front row seats", "bike rental",
]


def next_saturday() -> str:
    today = date.today()
    return (today + timedelta(days=(5 - today.weekday()) % 7 or 7)).strftime("%A %d %B")


def chat(session_id: str, history: list[dict], text: str, retries: int = 2) -> dict:
    history.append({"role": "user", "content": text})
    body = json.dumps({"session_id": session_id, "messages": history[-40:]}).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            f"{BASE}/api/v2/chat", data=body,
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        )
        try:
            out = json.load(urllib.request.urlopen(req, timeout=100))
        except (urllib.error.URLError, TimeoutError) as exc:
            out = {"reply": "", "error": f"transport: {exc}"}
        if not out.get("error"):
            break
        time.sleep(3 * (attempt + 1))
    history.append({"role": "assistant", "content": out.get("reply", "")})
    return out


def phase_a() -> int:
    when = next_saturday()
    print(f"PHASE A: availability for {when}, {len(ACTIVITIES)} activities\n")
    failures = 0
    for act in ACTIVITIES:
        t0 = time.time()
        out = chat(f"e2e-a-{act.replace(' ', '-')}-{int(t0)}", [], f"What are the price and available slots for {act} in Rishikesh on {when}?")
        reply = out.get("reply", "")
        problems = []
        if out.get("error"):
            problems.append(f"error={out['error']}")
        if not reply.strip():
            problems.append("empty reply")
        if "₹" not in reply:
            problems.append("no price in reply")
        failures += bool(problems)
        print(f"[{'FAIL' if problems else ' ok '}] {act:30} {time.time()-t0:5.1f}s {len(reply):5d} chars  {', '.join(problems)}")
        if problems:
            print(f"        reply: {reply[:200]!r}")
    print(f"\nPhase A: {len(ACTIVITIES) - failures}/{len(ACTIVITIES)} passed")
    return failures


def phase_b() -> int:
    when = next_saturday()
    sid, hist = f"e2e-b-{int(time.time())}", []
    show = lambda who, t: print(f"\n{who}: {t}")
    show("YOU", f"book {ACTIVITIES[0]} for {when}")
    out = chat(sid, hist, f"I want to book {ACTIVITIES[0]} in Rishikesh on {when} for 1 person, earliest slot.")
    show("BOT", out["reply"])
    phone = input("\nPhone number the bot asked for (a number you own, e.g. +91...): ").strip()
    out = chat(sid, hist, phone)
    show("BOT", out["reply"])
    otp = getpass.getpass("OTP you received by SMS (hidden): ").strip()
    out = chat(sid, hist, otp)
    show("BOT", out["reply"])
    for act in ACTIVITIES[1:]:
        out = chat(sid, hist, f"Also add {act} on {when} for 1 person, earliest slot, to my cart.")
        show("BOT", f"[{act}] {out['reply']}")
    out = chat(sid, hist, "Show me everything in my cart.")
    show("BOT", out["reply"])
    ok = "cart" in out["reply"].lower()
    if input("\nRemove everything from the cart now? [Y/n] ").strip().lower() != "n":
        show("BOT", chat(sid, hist, "Please remove everything from my cart.")["reply"])
    print(f"\nPhase B: cart step {'looks ok' if ok else 'DID NOT SHOW A CART'}. Review the replies above.")
    return 0 if ok else 1


if __name__ == "__main__":
    failed = phase_a()
    if "--book" in sys.argv:
        failed += phase_b()
    sys.exit(1 if failed else 0)
