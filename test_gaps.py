import urllib.request, json, time

BASE = "http://localhost:8080"

def post(path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=body, headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

# Gap 1a: invalid scope → must return HTTP 400
code, resp = post("/v1/context", {"scope": "INVALID", "context_id": "x", "version": 1, "payload": {}, "delivered_at": ""})
ok = code == 400 and resp.get("accepted") == False and resp.get("reason") == "invalid_scope"
print(f"[Gap 1a] Invalid scope  → HTTP {code} accepted={resp.get('accepted')} reason={resp.get('reason')}  {'PASS' if ok else 'FAIL'}")

# Gap 1b: stale version → must return HTTP 409
post("/v1/context", {"scope": "category", "context_id": "test_cat", "version": 5, "payload": {}, "delivered_at": ""})
code, resp = post("/v1/context", {"scope": "category", "context_id": "test_cat", "version": 3, "payload": {}, "delivered_at": ""})
ok = code == 409 and resp.get("accepted") == False and resp.get("reason") == "stale_version"
print(f"[Gap 1b] Stale version  → HTTP {code} accepted={resp.get('accepted')} reason={resp.get('reason')}  {'PASS' if ok else 'FAIL'}")

# Gap 1c: valid context → must return HTTP 200 + accepted=true
code, resp = post("/v1/context", {"scope": "category", "context_id": "test_cat2", "version": 1, "payload": {"slug": "test"}, "delivered_at": ""})
ok = code == 200 and resp.get("accepted") == True
print(f"[Gap 1c] Valid context  → HTTP {code} accepted={resp.get('accepted')}  {'PASS' if ok else 'FAIL'}")

# Gap 2: tick budget — empty tick must return in < 25s
t0 = time.time()
code, resp = post("/v1/tick", {"now": "2026-09-02T09:00:00Z", "available_triggers": []})
elapsed = time.time() - t0
ok = elapsed < 25 and code == 200
print(f"[Gap 2]  Tick budget    → {elapsed:.2f}s actions={len(resp.get('actions', []))}  {'PASS' if ok else 'FAIL'}")
