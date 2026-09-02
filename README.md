# Vera Bot — magicpin AI Challenge Submission

## Approach

**Architecture**: Stateful FastAPI server with a **decision-first** compose engine powered by Groq (`qwen/qwen3.8-27b`).

### Core design: decision before writing

Most bots dump all available context into a prompt and let the LLM figure out what to say. This bot does the opposite:

1. **Python selects ONE primary signal** per trigger kind (e.g. "CTR is 30% below peer median", "Smile Studio opened 1.3km away", "JIDA trial: 3-month recall 38% better")
2. **Python pre-computes all numbers** — CTR delta vs peer, months since last visit, estimated affected customers, days to wedding — so the LLM never fabricates stats
3. **LLM writes the message** around that single hook, using the right category voice

This maps directly to the 5 scoring dimensions:

| Dimension | How we address it |
|-----------|------------------|
| **Decision quality** | Python picks the sharpest signal per trigger; not a data dump |
| **Specificity** | All numbers derived from context (not LLM-invented): `38% better`, `30% below peer median`, `124 high-risk adults`, `1.3km away` |
| **Category fit** | Per-category domain vocabulary injected: dentists → `fluoride varnish, caries, recall interval`; restaurants → `covers, AOV, delivery radius`; gyms → `conversion, trial-to-paid, ad spend` |
| **Merchant fit** | Owner first name always used; live offers, review themes, and conversation history all referenced |
| **Engagement compulsion** | Contrarian judgment (IPL weekend → skip dine-in, push delivery), loss-aversion framing, single binary CTA |

### Key design decisions

1. **Signal selection layer**: Each of 25+ trigger kinds gets a `build_primary_signal()` call that extracts and frames the single best hook before any LLM call is made. For `research_digest` this is the trial_n + stat + merchant's patient cohort. For `ipl_match_today` it detects weeknight vs weekend and flips the recommendation accordingly.

2. **Trigger-kind routing**: `research_digest` → clinical/peer citation framing. `perf_dip` → data-anchored loss aversion. `recall_due` → slot-specific customer outreach. `competitor_opened` → voyeur-curiosity + lapsed customer reactivation. `active_planning_intent` → immediate execution (stop qualifying, start delivering).

3. **Auto-reply detection**: Pattern-match on canned WhatsApp Business phrases. First detection → flags for owner. Second → waits 4 hours. Third → ends conversation.

4. **Intent transitions**: Explicit yes/commit → switches to action mode immediately, delivers the draft/plan, stops re-qualifying. Opt-out → ends gracefully. Off-topic → one-line redirect.

5. **Suppression + anti-repetition**: Each `suppression_key` fires once per session. Last-sent body tracked per conversation to block verbatim repeats.

### Model choice

**Groq `qwen/qwen3.8-27b`** — ~1–2s latency per compose call, well within the 30s timeout even with 20 actions/tick. Temperature=0 for determinism.

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

# 3. Generate the dataset
python dataset/generate_dataset.py --seed-dir dataset --out dataset/expanded

# 4. Start the bot (or double-click start.bat on Windows)
uvicorn bot:app --host 0.0.0.0 --port 8080

# 5. Run smoke tests (11 checks, works against localhost or public URL)
python test_bot.py
python test_bot.py https://your-public-url.ngrok-free.app

# 6. Run message quality test (6 trigger kinds, shows actual LLM output)
python test_quality.py

# 7. Run the judge simulator
python judge_simulator.py
```

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/healthz` | Liveness probe — returns status, uptime, context counts, LLM model |
| `GET` | `/v1/metadata` | Bot identity, model, approach summary |
| `POST` | `/v1/context` | Receive category / merchant / customer / trigger context (idempotent, versioned) |
| `POST` | `/v1/tick` | Periodic wake-up — compose proactive messages for available triggers |
| `POST` | `/v1/reply` | Receive merchant/customer reply — return next action (send/wait/end) |
| `POST` | `/v1/teardown` | Wipe in-memory state (called by judge harness at start of each test run) |

---

## Files

```
bot.py               — Main FastAPI server (~520 lines, single file)
test_bot.py          — 11-check smoke test (runs against localhost or public URL)
test_quality.py      — LLM message quality test (6 trigger kinds + reply flow)
start.bat            — Windows: set API keys + start uvicorn
tunnel.bat           — Windows: open ngrok public tunnel on port 8080
dataset/             — Seed data + generator + expanded dataset (50 merchants, 200 customers, 100 triggers)
examples/            — API call examples + 10 scored case studies
```
