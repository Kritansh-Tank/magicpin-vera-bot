"""
Generate submission.jsonl — 30 pre-computed outputs from all seed triggers.
Run: python generate_submission.py
"""
import json, urllib.request, time
from pathlib import Path

BOT = "http://localhost:8080"
DATA = Path("dataset")
OUT  = Path("submission.jsonl")

def post(url, data):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=35) as r:
        return json.loads(r.read())

def push(scope, cid, v, payload):
    return post(f"{BOT}/v1/context", {
        "scope": scope, "context_id": cid, "version": v,
        "payload": payload, "delivered_at": ""
    })

# ── 1. Wipe and reload full base dataset ────────────────────────────────────
print("Wiping state and loading base dataset...")
post(f"{BOT}/v1/teardown", {})

# Categories
for f in (DATA / "categories").glob("*.json"):
    cat = json.loads(f.read_text(encoding="utf-8"))
    push("category", cat["slug"], 1, cat)
    print(f"  category/{cat['slug']}")

# Merchants
merchants = json.loads((DATA / "merchants_seed.json").read_text(encoding="utf-8"))["merchants"]
for m in merchants:
    push("merchant", m["merchant_id"], 1, m)
print(f"  {len(merchants)} merchants loaded")

# Customers
customers = json.loads((DATA / "customers_seed.json").read_text(encoding="utf-8"))["customers"]
for c in customers:
    push("customer", c["customer_id"], 1, c)
print(f"  {len(customers)} customers loaded")

# ── 2. Load triggers ─────────────────────────────────────────────────────────
triggers = json.loads((DATA / "triggers_seed.json").read_text(encoding="utf-8"))["triggers"]
print(f"\n{len(triggers)} triggers in seed — running all of them...\n")

# Push all triggers first
for trg in triggers:
    push("trigger", trg["id"], 1, trg)

# ── 3. Tick each trigger individually and collect output ─────────────────────
results = []
now_ts = "2026-09-02T09:00:00Z"

for i, trg in enumerate(triggers):
    t0 = time.time()
    resp = post(f"{BOT}/v1/tick", {
        "now": now_ts,
        "available_triggers": [trg["id"]]
    })
    actions = resp.get("actions", [])
    elapsed = time.time() - t0

    if actions:
        a = actions[0]
        row = {
            "test_id": f"T{i+1:02d}",
            "trigger_id": trg["id"],
            "trigger_kind": trg.get("kind", ""),
            "merchant_id": trg.get("merchant_id", ""),
            "customer_id": trg.get("customer_id"),
            "body": a.get("body", ""),
            "cta": a.get("cta", ""),
            "send_as": a.get("send_as", ""),
            "suppression_key": a.get("suppression_key", trg.get("suppression_key", "")),
            "rationale": a.get("rationale", ""),
        }
        results.append(row)
        print(f"[T{i+1:02d}] {trg['kind']:30s} → {len(a.get('body','').split()):2d} words  ({elapsed:.1f}s)")
        print(f"       {a.get('body','')[:100]}...")
    else:
        print(f"[T{i+1:02d}] {trg['kind']:30s} → NO ACTION (suppressed or error) ({elapsed:.1f}s)")
    print()

# ── 4. Write JSONL ────────────────────────────────────────────────────────────
with open(OUT, "w", encoding="utf-8") as f:
    for row in results:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

print(f"\n✅ submission.jsonl written — {len(results)} lines")
print(f"   (out of {len(triggers)} triggers; {len(triggers)-len(results)} suppressed/errored)")
