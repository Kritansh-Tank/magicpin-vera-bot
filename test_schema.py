import urllib.request, json

BASE = "http://localhost:8080"

def post(path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=body, headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def get(path):
    req = urllib.request.Request(f"{BASE}{path}")
    r = urllib.request.urlopen(req, timeout=5)
    return r.status, json.loads(r.read())

post("/v1/teardown", {})

# --- IDEMPOTENCY CHECK ---
# Same version re-post = no-op → brief says 200 accepted:true
post("/v1/context", {"scope": "category", "context_id": "dentists", "version": 3, "payload": {"slug": "dentists"}, "delivered_at": ""})
code, resp = post("/v1/context", {"scope": "category", "context_id": "dentists", "version": 3, "payload": {"slug": "dentists"}, "delivered_at": ""})
expected = code == 200 and resp.get("accepted") == True
print(f"[1] Same-version idempotency  HTTP {code} accepted={resp.get('accepted')}  {'PASS' if expected else 'FAIL (should be 200/True)'}")

# Lower version = 409
code, resp = post("/v1/context", {"scope": "category", "context_id": "dentists", "version": 1, "payload": {}, "delivered_at": ""})
expected = code == 409 and resp.get("accepted") == False
print(f"[2] Lower version stale        HTTP {code} accepted={resp.get('accepted')}  {'PASS' if expected else 'FAIL'}")

# Higher version = 200 replace
code, resp = post("/v1/context", {"scope": "category", "context_id": "dentists", "version": 5, "payload": {"slug": "dentists"}, "delivered_at": ""})
expected = code == 200 and resp.get("accepted") == True
print(f"[3] Higher version replace     HTTP {code} accepted={resp.get('accepted')}  {'PASS' if expected else 'FAIL'}")

# --- TICK ACTION SCHEMA ---
# Push minimal merchant + trigger
post("/v1/context", {"scope": "merchant", "context_id": "m_test", "version": 1,
    "payload": {"merchant_id": "m_test", "category_slug": "dentists",
                "identity": {"name": "Test Clinic", "languages": ["en"]},
                "subscription": {"status": "active", "plan": "Pro", "days_remaining": 30},
                "performance": {"views": 1000, "calls": 10, "ctr": 0.02, "delta_7d": {"views_pct": 0, "calls_pct": 0}},
                "offers": [], "signals": [], "customer_aggregate": {}}, "delivered_at": ""})
post("/v1/context", {"scope": "trigger", "context_id": "trg_test", "version": 1,
    "payload": {"id": "trg_test", "scope": "merchant", "kind": "perf_dip",
                "merchant_id": "m_test", "customer_id": None,
                "suppression_key": "test:perf:001", "urgency": 3}, "delivered_at": ""})

import time
t0 = time.time()
code, resp = post("/v1/tick", {"now": "2026-09-02T09:00:00Z", "available_triggers": ["trg_test"]})
elapsed = time.time() - t0

REQUIRED_FIELDS = ["conversation_id", "merchant_id", "customer_id", "send_as",
                    "trigger_id", "template_name", "template_params",
                    "body", "cta", "suppression_key", "rationale"]
actions = resp.get("actions", [])
if actions:
    a = actions[0]
    missing = [f for f in REQUIRED_FIELDS if f not in a]
    empty_body = not a.get("body", "").strip()
    print(f"[4] Tick action schema        fields={len(REQUIRED_FIELDS)-len(missing)}/{len(REQUIRED_FIELDS)}  body_len={len(a.get('body','').split())}w  elapsed={elapsed:.1f}s  {'PASS' if not missing and not empty_body else 'FAIL missing='+str(missing)}")
    if missing:
        print(f"    MISSING FIELDS: {missing}")
else:
    print(f"[4] Tick action schema        NO ACTIONS returned in {elapsed:.1f}s")

# --- REPLY SCHEMA ---
code, resp = post("/v1/reply", {"conversation_id": "conv_test_01", "merchant_id": "m_test",
    "customer_id": None, "from_role": "merchant", "message": "Yes let's do it",
    "received_at": "2026-09-02T09:01:00Z", "turn_number": 2})
REPLY_FIELDS = ["action", "rationale"]
missing = [f for f in REPLY_FIELDS if f not in resp]
print(f"[5] Reply schema              action={resp.get('action')} has_body={bool(resp.get('body'))}  {'PASS' if not missing else 'FAIL missing='+str(missing)}")

# --- HEALTHZ SCHEMA ---
code, resp = get("/v1/healthz")
HZ_FIELDS = ["status", "uptime_seconds", "contexts_loaded"]
missing = [f for f in HZ_FIELDS if f not in resp]
ctx = resp.get("contexts_loaded", {})
ctx_keys = set(ctx.keys()) >= {"category", "merchant", "customer", "trigger"}
print(f"[6] Healthz schema            status={resp.get('status')} ctx_keys={ctx_keys}  {'PASS' if not missing and ctx_keys else 'FAIL'}")

# --- METADATA SCHEMA ---
code, resp = get("/v1/metadata")
META_FIELDS = ["team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"]
missing = [f for f in META_FIELDS if f not in resp]
blank = [f for f in META_FIELDS if f in resp and not resp[f]]
print(f"[7] Metadata schema           missing={missing} blank={blank}  {'PASS' if not missing and not blank else 'FAIL'}")
