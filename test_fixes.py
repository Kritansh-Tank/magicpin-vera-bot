"""Quick test: verify Hinglish enforcement + customer-scope persona."""
import json, urllib.request, time
from pathlib import Path

BOT = "http://localhost:8080"
DATA = Path("dataset")

def post(url, data):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=35) as r:
        return json.loads(r.read())

def push(scope, cid, v, payload):
    return post(f"{BOT}/v1/context", {
        "scope": scope, "context_id": cid, "version": v,
        "payload": payload, "delivered_at": ""
    })

# Wipe state
post(f"{BOT}/v1/teardown", {})

cat = json.loads((DATA / "categories" / "dentists.json").read_text(encoding="utf-8"))
merch_seed = json.loads((DATA / "merchants_seed.json").read_text(encoding="utf-8"))
cust_seed  = json.loads((DATA / "customers_seed.json").read_text(encoding="utf-8"))
trg_seed   = json.loads((DATA / "triggers_seed.json").read_text(encoding="utf-8"))

meera = merch_seed["merchants"][0]
priya = cust_seed["customers"][0]

push("category", "dentists", 1, cat)
push("merchant", meera["merchant_id"], 1, meera)
push("customer", priya["customer_id"], 1, priya)

print("=" * 65)
print("LANGUAGE + PERSONA FIX VERIFICATION")
print("=" * 65)

# ── Test 1: research_digest → must be Hinglish (Dr. Meera speaks Hindi) ──
trg = trg_seed["triggers"][0]
push("trigger", trg["id"], 1, trg)
t0 = time.time()
r = post(f"{BOT}/v1/tick", {"now": "2026-09-02T09:00:00Z", "available_triggers": [trg["id"]]})
a = r["actions"][0] if r["actions"] else {}
body1 = a.get("body", "")
elapsed1 = time.time() - t0

print(f"\n[TEST 1: research_digest — expect HINGLISH] ({elapsed1:.1f}s)")
print(f"BODY: {body1}")
print(f"send_as={a.get('send_as')} | cta={a.get('cta')}")
hindi_words = ["hai", "aap", "mein", "ka", "ke", "hain", "karo", "karna", "naya", "aapke", "bhi"]
has_hinglish = any(w in body1.lower() for w in hindi_words)
print(f"CHECK Hinglish: {'PASS' if has_hinglish else 'FAIL — still full English'}")
words = len(body1.split())
print(f"CHECK <=55 words: {'PASS' if words <= 55 else f'FAIL — {words} words'}")

# ── Test 2: recall_due customer-scope → must be merchant_on_behalf, personal ──
recall_trg = {
    "id": "trg_test_recall_priya",
    "scope": "customer",
    "kind": "recall_due",
    "source": "internal",
    "merchant_id": meera["merchant_id"],
    "customer_id": priya["customer_id"],
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
    "suppression_key": "recall:priya:2026-Sep",
    "expires_at": "2026-09-08T00:00:00Z"
}
push("trigger", "trg_test_recall_priya", 1, recall_trg)
t0 = time.time()
r2 = post(f"{BOT}/v1/tick", {
    "now": "2026-09-02T09:05:00Z",
    "available_triggers": ["trg_test_recall_priya"]
})
a2 = r2["actions"][0] if r2["actions"] else {}
body2 = a2.get("body", "")
elapsed2 = time.time() - t0

print(f"\n[TEST 2: recall_due (customer-scope)] ({elapsed2:.1f}s)")
print(f"BODY: {body2}")
print(f"send_as={a2.get('send_as')} | cta={a2.get('cta')}")

cust_name = priya.get("identity", {}).get("name", "Priya")
cust_first = cust_name.split()[0]
print(f"CHECK send_as=merchant_on_behalf: {'PASS' if a2.get('send_as') == 'merchant_on_behalf' else 'FAIL'}")
print(f"CHECK customer name ({cust_first}) in body: {'PASS' if cust_first in body2 else 'FAIL'}")
words2 = len(body2.split())
print(f"CHECK <=55 words: {'PASS' if words2 <= 55 else f'FAIL — {words2} words'}")
no_vera_ref = "vera" not in body2.lower() and "AI assistant" not in body2
print(f"CHECK no Vera self-reference: {'PASS' if no_vera_ref else 'FAIL — mentions Vera/AI'}")

print("\n" + "=" * 65)
