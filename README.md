# Vera Bot — magicpin AI Challenge Submission

## Approach

**Architecture**: Stateful FastAPI server with a **decision-first** compose engine powered by Groq (`qwen/qwen3.8-27b`).

### Core design: decision before writing

Most bots dump all available context into a prompt and let the LLM figure out what to say. This bot does the opposite:

1. **Python selects ONE primary signal** per trigger kind (e.g. `CTR is 30% below peer median`, `Smile Studio opened 1.3km away`, `JIDA trial: 3-month recall 38% better`)
2. **Python pre-computes all numbers** — CTR delta vs peer, months since last visit, estimated affected customers, days to wedding — so the LLM never fabricates stats
3. **LLM writes the message** around that single hook, using the right category voice

This maps directly to the 5 scoring dimensions:

| Dimension | How we address it |
|-----------|------------------|
| **Specificity** | All numbers derived from context (never LLM-invented): `38% better`, `30% below peer median`, `124 high-risk adults`, `1.3km away`, `n=2100` |
| **Category fit** | Per-category domain vocabulary injected: dentists → `fluoride varnish, caries, recall interval`; restaurants → `covers, AOV, delivery radius`; gyms → `conversion, trial-to-paid, ad spend`; pharmacies → `chronic Rx, refill window, batch recall` |
| **Merchant fit** | Owner first name always used; live offers, review themes, social proof vs peer cohort, and conversation history all referenced |
| **Trigger relevance** | Every message explicitly surfaces *why now* — the trigger kind is part of the rationale and the hook sentence anchors on the triggering event |
| **Engagement compulsion** | Contrarian judgment (IPL weekend → push delivery not dine-in), loss-aversion framing, social proof percentile (`CTR bottom 30% of Delhi peers`), single binary CTA |

### Key design decisions

1. **Signal selection layer**: Each of 25+ trigger kinds gets a `build_primary_signal()` call that extracts and frames the single best hook before any LLM call. For `research_digest` this is the trial_n + stat + merchant's patient cohort. For `ipl_match_today` it detects weeknight vs weekend and flips the recommendation accordingly.

2. **Trigger-kind routing**: `research_digest` → clinical/peer citation framing. `perf_dip` → data-anchored loss aversion. `recall_due` → slot-specific customer outreach from the merchant (persona injection). `competitor_opened` → voyeur-curiosity + lapsed customer reactivation. `active_planning_intent` → immediate execution (stop qualifying, start delivering).

3. **Persona injection for customer-scope triggers**: When `scope=customer`, the compose prompt switches from Vera-as-assistant to merchant-as-sender. The LLM writes as Dr. Meera (or Lakshmi, or Karthik) texting their own patient/customer directly. `send_as=merchant_on_behalf` is enforced.

4. **Social proof**: A computed `social_proof_metric` field tells the LLM where the merchant sits in their peer cohort (`CTR 30% below Delhi solo practice median — bottom tier`). This surfaces the brief's most under-used compulsion lever (#3).

5. **Language handling**: Primary-language-sensitive. Hindi-primary merchants (`["hi", "en"]`) get mandatory Hinglish with examples. English-primary bilingual merchants (`["en", "hi"]`) get English with Hinglish welcome. Customer language overrides for customer-scope messages.

6. **Auto-reply detection**: Pattern-match on canned WhatsApp Business phrases. First detection → flags for owner with minimal ask. Second → ends conversation gracefully (Pattern B from brief).

7. **Intent transitions**: Explicit yes/commit → switches to action mode immediately, delivers the draft/plan, stops re-qualifying. Opt-out → ends. Off-topic → one-line redirect.

8. **Suppression + anti-repetition**: Each `suppression_key` fires once per session. Last-sent body tracked per conversation to block verbatim repeats (`-2` penalty per repeat).

9. **30s tick budget**: `/v1/tick` tracks elapsed time and returns early with whatever actions are ready if the 25s budget is hit — ensuring it never exceeds the judge's 30s timeout even under Groq rate-limit retries.

10. **Correct HTTP status codes**: `/v1/context` returns `400` for invalid scope and `409` for stale version (per brief §2.1 spec), not a generic 200 with `accepted: false`.

### Judge simulator results (pre-submission)

```
[PASS] warmup        — healthz + metadata verified
[PASS] auto_reply    — detects WA Business auto-reply, exits on 2nd occurrence
[PASS] intent        — switches to ACTION mode on "Ok lets do it"
[PASS] hostile       — ends gracefully on "Stop messaging me. This is spam."
```

### Model choice

**Groq `qwen/qwen3.8-27b`** — ~1–2s latency per compose call, well within the 30s timeout even at 20 actions/tick. Temperature=0 for determinism.

### What additional context would have helped most

1. **Real merchant conversation transcripts** — to calibrate the Hindi-English ratio and vocabulary preferred in each category-city combination.
2. **Historical engagement rates by trigger kind** — to rank which triggers are worth firing vs suppressing when multiple are queued.
3. **Real open slot data** — recall/appointment messages would be much more specific with actual calendar availability from the merchant's booking system.
4. **Regional dialect patterns** — Hyderabad Telugu merchants use different Hinglish structures than Delhi or Mumbai merchants.

---

## Setup

```bash
# 1. Install dependencies
pip install fastapi uvicorn groq

# 2. Set your Groq API key and model
export GROQ_API_KEY=gsk_...          # Linux/Mac
set GROQ_API_KEY=gsk_...             # Windows CMD
$env:GROQ_API_KEY=gsk_...            # Windows PowerShell
$env:GROQ_MODEL=qwen/qwen3.8-27b    # optional override

# 3. Start the bot (or double-click start.bat on Windows)
uvicorn bot:app --host 0.0.0.0 --port 8080

# 4. Run smoke tests (11 checks, works against localhost or public URL)
python test_bot.py
python test_bot.py https://your-public-url.ngrok-free.app

# 5. Run message quality test (7 trigger kinds, shows actual LLM output)
python test_quality.py

# 6. Run the judge simulator (set GROQ_API_KEY env var first)
python judge_simulator.py

# 7. Re-generate submission.jsonl (runs bot against all 25 seed triggers)
python generate_submission.py
```

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/healthz` | Liveness probe — returns status, uptime, context counts, LLM model |
| `GET` | `/v1/metadata` | Bot identity, model, approach summary |
| `POST` | `/v1/context` | Receive context (idempotent, versioned) — `200` accepted, `409` stale version, `400` invalid scope |
| `POST` | `/v1/tick` | Periodic wake-up — compose proactive messages; 25s budget, returns early if approaching 30s judge timeout |
| `POST` | `/v1/reply` | Receive merchant/customer reply — return next action (send/wait/end) |
| `POST` | `/v1/teardown` | Wipe in-memory state (called by judge harness at start/end of each test run) |

---

## Files

```
bot.py                  — Main FastAPI server (decision-first engine, ~1200 lines)
submission.jsonl        — 25 pre-computed outputs for all seed triggers (avg 38 words, 0 over limit)
generate_submission.py  — Script to re-generate submission.jsonl against a live bot
judge_simulator.py      — magicpin's LLM-powered judge (configured for Groq)
check_submission.py     — Quick quality check: word counts, CTA coverage, send_as audit
test_bot.py             — 11-check smoke test (structural + endpoint validation)
test_quality.py         — Message quality test (7 trigger kinds + reply flow)
test_fixes.py           — Language + persona fix verification (Hinglish, merchant_on_behalf)
test_gaps.py            — Verifies HTTP 400/409 status codes and tick budget (4/4 PASS)
start.bat               — Windows: set API keys + start uvicorn
tunnel.bat              — Windows: open ngrok public tunnel on port 8080
dataset/                — Seed data: 5 categories, 10 merchants, 15 customers, 25 triggers
examples/               — API call examples + 10 scored case studies
```
