#!/usr/bin/env python3
"""
Quick local smoke-test for the Vera bot.
Run with: python test_bot.py [BOT_URL]
Default BOT_URL: http://localhost:8080
"""
import sys, json, time, urllib.request, urllib.error
from pathlib import Path

BOT_URL = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8080"
DATASET_DIR = Path(__file__).parent / "dataset"
# Add a small pause between requests when hitting a public tunnel to avoid rate-limits
INTER_REQUEST_DELAY = 0.5 if BOT_URL.startswith("https") else 0

OK   = "[PASS]"
FAIL = "[FAIL]"
INFO = "[INFO]"


def post(url, data):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    for attempt in range(3):
        try:
            time.sleep(INTER_REQUEST_DELAY)
            with urllib.request.urlopen(req, timeout=35) as resp:
                return json.loads(resp.read()), resp.status
        except urllib.error.HTTPError as e:
            return json.loads(e.read()), e.code
        except Exception as e:
            if attempt == 2:
                return {"error": str(e)}, 0
            time.sleep(1)


def get(url):
    for attempt in range(3):
        try:
            time.sleep(INTER_REQUEST_DELAY)
            with urllib.request.urlopen(url, timeout=10) as resp:
                return json.loads(resp.read()), resp.status
        except Exception as e:
            if attempt == 2:
                return {"error": str(e)}, 0
            time.sleep(1)


def check(label, condition, detail=""):
    status = OK if condition else FAIL
    print(f"  {status} {label}" + (f" - {detail}" if detail else ""))
    return condition


def push_context(scope, context_id, version, payload):
    result, code = post(f"{BOT_URL}/v1/context", {
        "scope": scope,
        "context_id": context_id,
        "version": version,
        "payload": payload,
        "delivered_at": "2026-09-01T10:00:00Z",
    })
    return result, code


print(f"\n{'='*60}")
print(f"  Vera Bot Smoke Test — {BOT_URL}")
print(f"{'='*60}\n")

# Wipe state first so tests are idempotent
try:
    post(f"{BOT_URL}/v1/teardown", {})
    print("[INFO] State wiped via /v1/teardown\n")
except Exception:
    pass

# ── Test 1: Healthz ──────────────────────────────────────────────────────────
print("1. GET /v1/healthz")
result, code = get(f"{BOT_URL}/v1/healthz")
check("Status 200", code == 200, f"got {code}")
check("status=ok", result.get("status") == "ok")
check("contexts_loaded present", "contexts_loaded" in result)
print(f"  {INFO} LLM: {result.get('llm', 'unknown')}\n")

# ── Test 2: Metadata ─────────────────────────────────────────────────────────
print("2. GET /v1/metadata")
result, code = get(f"{BOT_URL}/v1/metadata")
check("Status 200", code == 200)
check("team_name present", "team_name" in result)
check("model present", "model" in result)
print()

# ── Test 3: Context Push — Category ──────────────────────────────────────────
print("3. POST /v1/context — Category")
cat_file = DATASET_DIR / "categories" / "dentists.json"
cat_payload = json.loads(cat_file.read_text(encoding="utf-8"))
result, code = push_context("category", "dentists", 1, cat_payload)
check("Accepted", result.get("accepted") is True, f"ack_id={result.get('ack_id')}")

# Idempotency: re-push same version → stale_version
result2, code2 = push_context("category", "dentists", 1, cat_payload)
check("Re-push same version → stale_version", result2.get("reason") == "stale_version")

# Version bump → accepted
result3, code3 = push_context("category", "dentists", 2, cat_payload)
check("Version bump accepted", result3.get("accepted") is True)
print()

# ── Test 4: Context Push — Merchant ──────────────────────────────────────────
print("4. POST /v1/context — Merchant")
merchants_seed = json.loads((DATASET_DIR / "merchants_seed.json").read_text(encoding="utf-8"))
meera = merchants_seed["merchants"][0]
result, code = push_context("merchant", "m_001_drmeera_dentist_delhi", 1, meera)
check("Accepted", result.get("accepted") is True)
print()

# ── Test 5: Context Push — Trigger ───────────────────────────────────────────
print("5. POST /v1/context — Trigger")
trg_payload = {
    "id": "trg_001_research_digest_dentists",
    "scope": "merchant",
    "kind": "research_digest",
    "source": "external",
    "merchant_id": "m_001_drmeera_dentist_delhi",
    "customer_id": None,
    "payload": {"category": "dentists", "top_item_id": "d_2026W17_jida_fluoride"},
    "urgency": 2,
    "suppression_key": "research:dentists:2026-W17",
    "expires_at": "2026-05-03T00:00:00Z",
}
result, code = push_context("trigger", "trg_001_research_digest_dentists", 1, trg_payload)
check("Accepted", result.get("accepted") is True)
print()

# ── Test 6: Tick ──────────────────────────────────────────────────────────────
print("6. POST /v1/tick — expect action for research_digest trigger")
t0 = time.time()
result, code = post(f"{BOT_URL}/v1/tick", {
    "now": "2026-09-01T10:30:00Z",
    "available_triggers": ["trg_001_research_digest_dentists"],
})
elapsed = time.time() - t0
check("Status 200", code == 200)
check("actions list present", "actions" in result)
actions = result.get("actions", [])
check("At least 1 action", len(actions) >= 1, f"got {len(actions)}")
if actions:
    a = actions[0]
    check("body not empty", bool(a.get("body")))
    check("rationale present", bool(a.get("rationale")))
    check("suppression_key set", bool(a.get("suppression_key")))
    check("no URL in body", "http" not in a.get("body", "").lower(),
          "body contains http URL (penalty!)")
    print(f"\n  {INFO} Generated message preview:")
    print(f"     {a.get('body', '')[:200]}")
    print(f"     CTA: {a.get('cta')} | send_as: {a.get('send_as')}")
    conv_id = a.get("conversation_id")
check(f"Completed in <30s", elapsed < 30, f"{elapsed:.1f}s")
print()

# ── Test 7: Tick — same trigger → suppressed ──────────────────────────────────
print("7. POST /v1/tick — same trigger should be suppressed now")
result, code = post(f"{BOT_URL}/v1/tick", {
    "now": "2026-09-01T10:35:00Z",
    "available_triggers": ["trg_001_research_digest_dentists"],
})
actions2 = result.get("actions", [])
check("Empty actions (suppressed)", len(actions2) == 0, f"got {len(actions2)}")
print()

# ── Test 8: Reply — Engaged merchant ─────────────────────────────────────────
if actions:
    conv_id = a.get("conversation_id", "conv_test")
    print(f"8. POST /v1/reply — Engaged merchant reply (conv: {conv_id})")
    t0 = time.time()
    result, code = post(f"{BOT_URL}/v1/reply", {
        "conversation_id": conv_id,
        "merchant_id": "m_001_drmeera_dentist_delhi",
        "customer_id": None,
        "from_role": "merchant",
        "message": "Yes please send the abstract and draft the patient WhatsApp.",
        "received_at": "2026-09-01T10:42:00Z",
        "turn_number": 2,
    })
    elapsed = time.time() - t0
    check("Status 200", code == 200)
    check("action=send", result.get("action") == "send")
    check("body not empty", bool(result.get("body")))
    check("Completed in <30s", elapsed < 30, f"{elapsed:.1f}s")
    if result.get("body"):
        print(f"  {INFO} Reply: {result['body'][:200]}")
    print()

    # ── Test 9: Auto-reply detection ──────────────────────────────────────────
    print("9. POST /v1/reply — Auto-reply detection")
    result, code = post(f"{BOT_URL}/v1/reply", {
        "conversation_id": conv_id + "_auto",
        "merchant_id": "m_001_drmeera_dentist_delhi",
        "customer_id": None,
        "from_role": "merchant",
        "message": "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly.",
        "received_at": "2026-09-01T10:43:00Z",
        "turn_number": 2,
    })
    check("Status 200", code == 200)
    action = result.get("action")
    check("Not sending normal reply (should wait or send flagging message)",
          action in ("wait", "send"), f"action={action}")
    print()

    # ── Test 10: Hard opt-out ─────────────────────────────────────────────────
    print("10. POST /v1/reply — Hard opt-out")
    result, code = post(f"{BOT_URL}/v1/reply", {
        "conversation_id": conv_id + "_optout",
        "merchant_id": "m_001_drmeera_dentist_delhi",
        "customer_id": None,
        "from_role": "merchant",
        "message": "Stop messaging me. Not interested.",
        "received_at": "2026-09-01T10:44:00Z",
        "turn_number": 2,
    })
    check("Status 200", code == 200)
    check("action=end", result.get("action") == "end", f"got {result.get('action')}")
    print()

# ── Test 11: Healthz after loading ───────────────────────────────────────────
print("11. GET /v1/healthz — after context load")
result, code = get(f"{BOT_URL}/v1/healthz")
counts = result.get("contexts_loaded", {})
check("category=2 (v1 replaced by v2)", counts.get("category", 0) >= 1)
check("merchant>=1", counts.get("merchant", 0) >= 1)
check("trigger>=1", counts.get("trigger", 0) >= 1)
print(f"  {INFO} Contexts loaded: {counts}\n")

print(f"{'='*60}")
print("  Smoke test complete. Check for ✗ failures above.")
print(f"{'='*60}\n")
