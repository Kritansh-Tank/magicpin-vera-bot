"""Full quality test v2 — tests all key trigger kinds including IPL contrarian judgment."""
import json, time
import urllib.request
from pathlib import Path

BOT = "http://localhost:8080"
DATA = Path(__file__).parent / "dataset"


def post(url, data):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=35) as r:
        return json.loads(r.read())


def push(scope, cid, version, payload):
    return post(f"{BOT}/v1/context", {
        "scope": scope, "context_id": cid, "version": version,
        "payload": payload, "delivered_at": "2026-09-01T10:00:00Z"
    })


def show_action(trg_kind, actions, elapsed):
    print(f"\n[TRIGGER: {trg_kind}] ({elapsed:.1f}s)")
    if not actions:
        print("  (suppressed or skipped)")
        return
    a = actions[0]
    print(f"BODY:\n  {a['body']}")
    print(f"CTA: {a['cta']} | send_as: {a['send_as']}")
    print(f"RATIONALE: {a['rationale'][:120]}")
    print("-" * 70)


# Load seed data
cat = json.loads((DATA / "categories" / "dentists.json").read_text(encoding="utf-8"))
merch_seed = json.loads((DATA / "merchants_seed.json").read_text(encoding="utf-8"))
trg_seed = json.loads((DATA / "triggers_seed.json").read_text(encoding="utf-8"))
cust_seed = json.loads((DATA / "customers_seed.json").read_text(encoding="utf-8"))

meera = merch_seed["merchants"][0]  # Dr. Meera, dentist

# Load salons category + merchant too
sal_cat = json.loads((DATA / "categories" / "salons.json").read_text(encoding="utf-8"))
salon_merch = merch_seed["merchants"][5] if len(merch_seed["merchants"]) > 5 else merch_seed["merchants"][0]
rest_cat = json.loads((DATA / "categories" / "restaurants.json").read_text(encoding="utf-8"))
rest_merch = next((m for m in merch_seed["merchants"] if m.get("category_slug") == "restaurants"), meera)

# Push contexts
push("category", "dentists", 1, cat)
push("category", "salons", 1, sal_cat)
push("category", "restaurants", 1, rest_cat)
push("merchant", meera["merchant_id"], 1, meera)
if salon_merch.get("merchant_id") != meera["merchant_id"]:
    push("merchant", salon_merch["merchant_id"], 1, salon_merch)
if rest_merch.get("merchant_id") and rest_merch["merchant_id"] != meera["merchant_id"]:
    push("merchant", rest_merch["merchant_id"], 1, rest_merch)

# Sample customer
sample_cust = cust_seed["customers"][0]
push("customer", sample_cust["customer_id"], 1, sample_cust)

print("=" * 70)
print("QUALITY TEST v2 — Decision-First Engine")
print("=" * 70)

# === TEST 1: research_digest ===
trg = trg_seed["triggers"][0]
push("trigger", trg["id"], 1, trg)
t0 = time.time()
r = post(f"{BOT}/v1/tick", {"now": "2026-09-01T10:00:00Z", "available_triggers": [trg["id"]]})
show_action("research_digest", r.get("actions", []), time.time() - t0)

# === TEST 2: regulation_change ===
trg2 = trg_seed["triggers"][1]
push("trigger", trg2["id"], 1, trg2)
t0 = time.time()
r2 = post(f"{BOT}/v1/tick", {"now": "2026-09-01T10:05:00Z", "available_triggers": [trg2["id"]]})
show_action("regulation_change", r2.get("actions", []), time.time() - t0)

# === TEST 3: competitor_opened ===
trg23 = trg_seed["triggers"][22]
push("trigger", trg23["id"], 1, trg23)
t0 = time.time()
r3 = post(f"{BOT}/v1/tick", {"now": "2026-09-01T10:10:00Z", "available_triggers": [trg23["id"]]})
show_action("competitor_opened", r3.get("actions", []), time.time() - t0)

# === TEST 4: IPL match day (weekend = contrarian) ===
ipl_trg = {
    "id": "trg_test_ipl_weekend",
    "scope": "merchant",
    "kind": "ipl_match_today",
    "source": "external",
    "merchant_id": rest_merch["merchant_id"] if rest_merch.get("merchant_id") else meera["merchant_id"],
    "customer_id": None,
    "payload": {
        "match": "DC vs MI",
        "venue": "Arun Jaitley Stadium",
        "match_time_iso": "2026-09-01T19:30:00+05:30",
        "is_weeknight": False,  # WEEKEND — should trigger contrarian recommendation
        "expected_footfall_delta_pct": -0.12,
    },
    "urgency": 4,
    "suppression_key": "ipl:DC_vs_MI:2026-09-01",
    "expires_at": "2026-09-01T22:00:00Z"
}
push("trigger", ipl_trg["id"], 1, ipl_trg)
merch_for_ipl = rest_merch if rest_merch.get("merchant_id") else meera
t0 = time.time()
r4 = post(f"{BOT}/v1/tick", {
    "now": "2026-09-01T10:15:00Z",
    "available_triggers": ["trg_test_ipl_weekend"]
})
show_action("ipl_match_today (WEEKEND — should warn vs dine-in)", r4.get("actions", []), time.time() - t0)

# === TEST 5: perf_dip ===
dip_trg = {
    "id": "trg_test_perf_dip_meera",
    "scope": "merchant",
    "kind": "perf_dip",
    "source": "internal",
    "merchant_id": meera["merchant_id"],
    "customer_id": None,
    "payload": {"metric": "calls", "delta_pct": -0.41, "window": "7d", "vs_baseline": 18},
    "urgency": 4,
    "suppression_key": "perf_dip:meera:2026-W35:calls",
    "expires_at": "2026-09-07T00:00:00Z"
}
push("trigger", dip_trg["id"], 1, dip_trg)
t0 = time.time()
r5 = post(f"{BOT}/v1/tick", {"now": "2026-09-01T10:20:00Z", "available_triggers": ["trg_test_perf_dip_meera"]})
show_action("perf_dip (-41% calls)", r5.get("actions", []), time.time() - t0)

# === TEST 6: Reply flow ===
actions5 = r5.get("actions", [])
if actions5:
    conv_id = actions5[0]["conversation_id"]
    print(f"\n[REPLY FLOW] on conv {conv_id}")

    # Affirmative reply
    t0 = time.time()
    rep = post(f"{BOT}/v1/reply", {
        "conversation_id": conv_id,
        "merchant_id": meera["merchant_id"],
        "from_role": "merchant",
        "message": "Yes please, check what changed",
        "received_at": "2026-09-01T10:25:00Z",
        "turn_number": 2
    })
    print(f"  Merchant: 'Yes please, check what changed' → Vera ({time.time()-t0:.1f}s):")
    print(f"  {rep.get('body','?')}")
    print(f"  action={rep.get('action')} cta={rep.get('cta')}")

# === TEST 7: recall_due — customer-scope (merchant→patient) ===
recall_trg = {
    "id": "trg_test_recall_priya",
    "scope": "customer",          # KEY: customer scope → merchant_on_behalf
    "kind": "recall_due",
    "source": "internal",
    "merchant_id": meera["merchant_id"],
    "customer_id": sample_cust["customer_id"],
    "payload": {
        "service_due": "dental_cleaning",
        "last_service_date": "2026-03-01",
        "due_date": "2026-09-01",
        "available_slots": [
            {"label": "Sat 6 Sep, 11am"},
            {"label": "Mon 8 Sep, 6pm"},
        ]
    },
    "urgency": 3,
    "suppression_key": f"recall:{sample_cust['customer_id']}:2026-Sep",
    "expires_at": "2026-09-08T00:00:00Z"
}
push("trigger", recall_trg["id"], 1, recall_trg)
t0 = time.time()
r7 = post(f"{BOT}/v1/tick", {
    "now": "2026-09-02T09:00:00Z",
    "available_triggers": ["trg_test_recall_priya"]
})
show_action(
    "recall_due (CUSTOMER-SCOPE → should be from Dr. Meera to Priya, Hinglish)",
    r7.get("actions", []), time.time() - t0
)

print("\n" + "=" * 70)
print("KEY CHECKS:")
print("  ✅ research_digest/regulation_change → Hinglish? (not full English)")
print("  ✅ recall_due → send_as=merchant_on_behalf? (from Dr. Meera to patient)")
print("  ✅ recall_due → personal tone? (not corporate/bot)")
print("  ✅ All messages ≤55 words?")
print("=" * 70)
