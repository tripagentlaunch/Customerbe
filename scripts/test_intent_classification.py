"""One-off accuracy harness for Aanya's tool-selection: sends a fixed test
set of realistic member messages to a running /ai/concierge/chat endpoint,
grades each against an expected outcome, and reports accuracy + every miss.

This tests the REAL, live decision (Claude's tool_choice: auto, and the
deterministic human-handoff regex in ai_router.py) — not a mock. Requires a
running backend with real ANTHROPIC_API_KEY (and ideally PINECONE_API_KEY,
though retrieval failing gracefully degrades to ungrounded answers rather
than blocking tool selection).

Run:
    python scripts/test_intent_classification.py [--url http://localhost:8000]

Each case gets its own fresh session_id (uuid) so pending-confirmation state
from one case can never leak into another.
"""

import argparse
import json
import sys
import uuid
from pathlib import Path
from urllib import error, request

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# (category, message, expected_tool_or_None, expected_handoff_kind_or_None, note)
# expected_tool: a tool name that MUST appear in tools_called, or None if no
#   tool should fire this turn (research answers, incomplete booking asks
#   that need clarification first, ambiguous/greeting messages).
# expected_handoff_kind: "advisor_prompt" if this should hit the deterministic
#   human-handoff path (regex in ai_router.py, not a tool at all).
TEST_CASES = [
    # --- research: answer from the corpus, no tool ---
    ("research", "what's Dubai like in December", None, None, "pure destination research"),
    ("research", "is Bali good for a honeymoon?", None, None, "pure destination research"),
    ("research", "hows the food scene in bangkok", None, None, "casual/no-caps phrasing"),
    ("research", "best time to visit japan", None, None, "pure destination research"),
    ("research", "is zermatt walkable without a car", None, None, "pure destination research"),

    # --- booking intent: complete info should call the search tool now;
    #     incomplete info should NOT call a tool yet (ask for missing fields) ---
    ("booking_complete", "book me a flight to dubai on the 12th of december from delhi",
     "search_flights", None, "origin+destination+date all present"),
    ("booking_incomplete", "I want to book a flight to Dubai",
     None, None, "missing dates/origin — should ask, not fabricate a search"),
    ("booking_complete", "flights from mumbai to singapore business class 3rd jan",
     "search_flights", None, "origin+destination+date all present"),
    ("booking_incomplete", "need a hotel in bali",
     None, None, "missing dates — should ask, not fabricate a search"),
    ("booking_complete", "find me a hotel in bali dec 10 to dec 15 for 2 adults",
     "search_hotels", None, "destination+dates+guests all present"),
    ("booking_visa", "do i need a visa for dubai",
     "check_visa_requirement", None, "visa fact — must be tool-grounded, not guessed"),
    ("booking_visa", "whats the visa process for bali for indians",
     "check_visa_requirement", None, "visa fact — must be tool-grounded, not guessed"),

    # --- ambiguous: short/vague, could go either way ---
    ("ambiguous", "dubai in december", None, None, "too short to tell research vs booking — should NOT guess a tool"),
    ("ambiguous", "hii", None, None, "bare greeting"),
    ("ambiguous", "thinking about a trip", None, None, "vague, no destination/dates"),
    ("ambiguous", "whats good in dec", None, None, "incomplete grammar, vague"),

    # --- human handoff: must hit the deterministic regex fast-path, not Claude ---
    ("human_handoff", "let me talk to a person", None, "advisor_prompt", "canonical phrasing"),
    ("human_handoff", "can i just speak to someone real", None, "advisor_prompt", "phrasing variant"),
    ("human_handoff", "human pls", None, "advisor_prompt", "casual/abbreviated"),
    ("human_handoff", "I'd like to speak with my advisor directly", None, "advisor_prompt", "canonical phrasing"),
]


def call_endpoint(base_url: str, query: str) -> dict:
    session_id = f"test-{uuid.uuid4()}"
    body = json.dumps({"query": query, "context": {}, "session_id": session_id}).encode()
    req = request.Request(
        f"{base_url}/ai/concierge/chat", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read())
    except error.HTTPError as exc:
        return {"_error": f"HTTP {exc.code}: {exc.read().decode(errors='replace')}"}
    except Exception as exc:  # noqa: BLE001
        return {"_error": f"{type(exc).__name__}: {exc}"}


def grade(case, response: dict) -> tuple[bool, str]:
    _, message, expected_tool, expected_handoff_kind, note = case

    if "_error" in response:
        return False, f"request failed: {response['_error']}"

    tools_called = response.get("tools_called") or []
    handoff = response.get("handoff") or {}
    handoff_kind = handoff.get("kind")

    if expected_handoff_kind:
        ok = handoff_kind == expected_handoff_kind and not tools_called
        detail = f"handoff.kind={handoff_kind!r}, tools_called={tools_called}"
        return ok, detail

    if expected_tool:
        ok = expected_tool in tools_called
        detail = f"tools_called={tools_called}"
        return ok, detail

    # expected_tool is None and no handoff expected: correct = no tool fired
    ok = not tools_called
    detail = f"tools_called={tools_called}"
    return ok, detail


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--out", default=None, help="optional path to dump raw results as JSON")
    args = parser.parse_args()

    results = []
    passed = 0
    for i, case in enumerate(TEST_CASES, 1):
        category, message, expected_tool, expected_handoff_kind, note = case
        print(f"[{i}/{len(TEST_CASES)}] ({category}) {message!r} ...", end=" ", flush=True)
        response = call_endpoint(args.url, message)
        ok, detail = grade(case, response)
        passed += ok
        status = "PASS" if ok else "FAIL"
        print(f"{status} — {detail}")
        results.append({
            "category": category, "message": message, "expected_tool": expected_tool,
            "expected_handoff_kind": expected_handoff_kind, "note": note,
            "response": response, "ok": ok, "detail": detail,
        })

    total = len(TEST_CASES)
    print(f"\n{'='*60}\nAccuracy: {passed}/{total} ({100*passed/total:.1f}%)\n{'='*60}")

    failures = [r for r in results if not r["ok"]]
    if failures:
        print(f"\n{len(failures)} misclassification(s):\n")
        for r in failures:
            print(f"- [{r['category']}] {r['message']!r}")
            print(f"    expected: tool={r['expected_tool']!r} handoff_kind={r['expected_handoff_kind']!r}  ({r['note']})")
            print(f"    actual:   {r['detail']}")
            intro = (r["response"].get("intro") or "")[:160]
            print(f"    reply:    {intro!r}")
            print()
    else:
        print("\nNo misclassifications.")

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2))
        print(f"\nFull results written to {args.out}")


if __name__ == "__main__":
    main()
