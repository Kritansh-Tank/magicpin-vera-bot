#!/usr/bin/env python3
"""
Vera Bot v2 — magicpin AI Challenge
====================================
Decision-first message engine: pick ONE sharp signal, then write around it.

Architecture:
  - Python computes all derived numbers before the LLM call
  - LLM sees a structured brief, not a data dump
  - Single JSON output per compose call (no two-phase for latency)
  - Trigger-kind routing with domain-specific context slices
  - Auto-reply / opt-out / intent detection in reply handler

Endpoints: POST /v1/context  POST /v1/tick  POST /v1/reply
           GET  /v1/healthz  GET  /v1/metadata

Usage:
  export GROQ_API_KEY=gsk_...
  export GROQ_MODEL=qwen/qwen3.8-27b   # or llama-3.3-70b-versatile
  uvicorn bot:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import os, re, time, json, math, logging
from datetime import datetime, timezone, date
from typing import Any, Optional
from fastapi import FastAPI
from pydantic import BaseModel
from groq import Groq

# ── Config ─────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vera-bot")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
USE_LLM      = bool(GROQ_API_KEY)
groq_client: Optional[Groq] = Groq(api_key=GROQ_API_KEY) if USE_LLM else None

START_TIME = time.time()
app = FastAPI(title="Vera Bot v2", version="2.0.0")

# ── State ──────────────────────────────────────────────────────────────────────
contexts: dict[tuple[str, str], dict]  = {}   # (scope, id) → {version, payload}
conversations: dict[str, list[dict]]   = {}   # conv_id → turns
suppressed: set[str]                   = set() # fired suppression_keys
ended_convs: set[str]                  = set()
auto_reply_counts: dict[str, int]      = {}
last_sent: dict[str, str]              = {}    # conv_id → last body
unanswered_sends: dict[str, int]       = {}    # merchant_id → consecutive unanswered outbound count

# ── Pydantic models ────────────────────────────────────────────────────────────
class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str = ""

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str = ""
    turn_number: int = 1

# ── Helpers ────────────────────────────────────────────────────────────────────
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def get_payload(scope: str, cid: str) -> Optional[dict]:
    e = contexts.get((scope, cid))
    return e["payload"] if e else None

def pct(val: float) -> str:
    sign = "+" if val >= 0 else ""
    return f"{sign}{val*100:.0f}%"

def months_since(date_str: str) -> Optional[int]:
    """Return approximate months between date_str (YYYY-MM-DD) and today."""
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        today = date.today()
        return (today.year - d.year) * 12 + (today.month - d.month)
    except Exception:
        return None

def days_until(date_str: str) -> Optional[int]:
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        return (d - date.today()).days
    except Exception:
        return None

# ── Auto-reply / intent patterns ───────────────────────────────────────────────
AUTO_REPLY_PHRASES = [
    "thank you for contacting", "will respond shortly", "out of office",
    "automated response", "this is an automated", "we'll get back to you",
    "aapki jaankari ke liye bahut-bahut shukriya", "main ek automated assistant hoon",
    "hamari team tak pahuncha deti hoon", "our office hours",
    "we are currently unavailable", "auto-reply",
]
OPT_OUT = [
    "stop", "not interested", "don't contact", "do not contact", "remove me",
    "unsubscribe", "band karo", "mat bhejo", "irritating", "spam",
    "stop messaging", "leave me alone", "useless", "bothering",
    "mujhe message mat karo",
]
EXPLICIT_YES = [
    "let's do it", "lets do it", "ok go ahead", "yes go ahead", "haan",
    "yes please", "yes do it", "proceed", "confirm", "chalega", "kar do",
    "bata do", "sure go ahead", "go for it", "ok let's", "ok lets",
    "chalo karo", "theek hai", "bilkul", "zaroor",
]

def is_auto_reply(msg: str) -> bool:
    return any(p in msg.lower() for p in AUTO_REPLY_PHRASES)

def is_opt_out(msg: str) -> bool:
    return any(p in msg.lower() for p in OPT_OUT)

def is_explicit_yes(msg: str) -> bool:
    ml = msg.lower().strip()
    if ml in {"yes", "y", "ok", "okay", "sure", "haan", "ha", "yep", "yup",
               "go", "do it", "karo", "accha", "theek"}:
        return True
    return any(p in ml for p in EXPLICIT_YES)

# ── DOMAIN VOCABULARY by category ─────────────────────────────────────────────
CATEGORY_VOCAB = {
    "dentists": {
        "domain_words": "fluoride varnish, caries, recall interval, scaling, occlusion, bruxism, OPG, IOPA, RCT, CAD/CAM, zirconia, endodontic, periodontal, aligner, veneer",
        "avoid": "guaranteed, 100% safe, completely cure, miracle, best in city",
        "tone": "peer-clinical. Talk like one dentist to another. Cite sources (journal, page). No overclaims.",
        "offer_format": "service+price (Dental Cleaning @ ₹299), not flat-% discounts",
    },
    "salons": {
        "domain_words": "balayage, keratin, bridal trial, skin-prep, blow-dry, highlights, color correction, styling, manicure, pedicure",
        "avoid": "guaranteed, 100% results, miracle treatment",
        "tone": "warm-operator. Fellow salon owner talking. Emojis OK (sparingly). Visual language.",
        "offer_format": "service+price (Haircut @ ₹99, Hair Spa @ ₹499)",
    },
    "restaurants": {
        "domain_words": "covers, AOV (average order value), delivery radius, dine-in, thali, match-night, footfall, Swiggy/Zomato, corporate bulk",
        "avoid": "guaranteed, amazing, best in city",
        "tone": "operator-to-operator. Direct. Uses restaurant industry language. Data-first.",
        "offer_format": "BOGO, set-meal pricing, timed offers (Buy 1 Get 1 Tue-Thu)",
    },
    "gyms": {
        "domain_words": "conversion, trial-to-paid, churn rate, ad spend, members, HIIT, body composition, resistance, cardio, retention",
        "avoid": "guaranteed results, 100% effective",
        "tone": "coach-to-operator. Energetic but data-grounded. Uses acquisition/retention language.",
        "offer_format": "trial offers (3 FREE trial classes, First Month @ ₹499)",
    },
    "pharmacies": {
        "domain_words": "molecule, batch, chronic-Rx, sub-potency, dispensed, refill, ORS, antifungal, free home delivery, senior discount",
        "avoid": "guaranteed, 100% safe, cure",
        "tone": "trustworthy-precise. Respectful. Calm. No alarm. Clinical accuracy matters.",
        "offer_format": "service+offer (Free Home Delivery >₹499, Senior 15% OFF)",
    },
}

# ── DERIVED CONTEXT BUILDER ───────────────────────────────────────────────────
def build_derived(merchant: dict, category: dict, trigger: dict, customer: Optional[dict]) -> dict:
    """
    Pre-compute all numbers Python can derive so the LLM doesn't need to.
    Returns a dict of named derived values used in the prompt.
    """
    d: dict[str, Any] = {}

    # Merchant basics
    ident = merchant.get("identity", {})
    d["merchant_name"] = ident.get("name", "Merchant")
    d["owner_first"] = ident.get("owner_first_name", d["merchant_name"])
    d["locality"] = ident.get("locality", "")
    d["city"] = ident.get("city", "")
    d["verified"] = ident.get("verified", False)
    d["languages"] = ident.get("languages", ["en"])
    d["established_year"] = ident.get("established_year", "")
    d["cat_slug"] = merchant.get("category_slug", category.get("slug", ""))

    # Subscription
    sub = merchant.get("subscription", {})
    d["sub_status"] = sub.get("status", "unknown")
    d["sub_plan"] = sub.get("plan", "")
    d["sub_days_left"] = sub.get("days_remaining", 0)
    d["sub_days_since_expiry"] = sub.get("days_since_expiry", 0)

    # Performance
    perf = merchant.get("performance", {})
    peer = category.get("peer_stats", {})
    d["views_30d"] = perf.get("views", 0)
    d["calls_30d"] = perf.get("calls", 0)
    d["ctr"] = perf.get("ctr", 0)
    d["peer_ctr"] = peer.get("avg_ctr", 0)
    d["peer_avg_reviews"] = peer.get("avg_review_count", 0)
    d["peer_avg_views"] = peer.get("avg_views_30d", 0)
    delta = perf.get("delta_7d", {})
    d["views_7d_delta"] = delta.get("views_pct", 0)
    d["calls_7d_delta"] = delta.get("calls_pct", 0)
    # CTR vs peer — signed percentage points
    if d["peer_ctr"] > 0:
        d["ctr_vs_peer_pct"] = round((d["ctr"] - d["peer_ctr"]) / d["peer_ctr"] * 100)
        d["ctr_vs_peer_label"] = "above" if d["ctr"] >= d["peer_ctr"] else "below"
    else:
        d["ctr_vs_peer_pct"] = 0
        d["ctr_vs_peer_label"] = "at"

    # Offers
    all_offers = merchant.get("offers", [])
    d["active_offers"] = [o for o in all_offers if o.get("status") == "active"]
    d["expired_offers"] = [o for o in all_offers if o.get("status") == "expired"]
    d["active_offer_titles"] = [o.get("title", "") for o in d["active_offers"]]
    # Category catalog fallback
    d["cat_offers"] = [o.get("title", "") for o in category.get("offer_catalog", [])[:4]]

    # Customer aggregate
    agg = merchant.get("customer_aggregate", {})
    d["total_customers_ytd"] = agg.get("total_unique_ytd", 0)
    d["lapsed_180d"] = agg.get("lapsed_180d_plus", 0)
    d["lapsed_90d"] = agg.get("lapsed_90d_plus", 0)
    d["retention_6mo"] = agg.get("retention_6mo_pct", 0)
    d["retention_3mo"] = agg.get("retention_3mo_pct", 0)
    d["high_risk_adults"] = agg.get("high_risk_adult_count", 0)
    d["chronic_rx_count"] = agg.get("chronic_rx_count", 0)
    d["active_members"] = agg.get("total_active_members", 0)
    d["monthly_churn"] = agg.get("monthly_churn_pct", 0)
    d["trial_to_paid"] = agg.get("trial_to_paid_pct", 0)
    d["repeat_pct"] = agg.get("repeat_customer_pct", 0)

    d["signals"] = merchant.get("signals", [])

    # Social proof — LLM can use this to frame competitive comparisons
    peer_ctr = d["peer_ctr"]
    merch_ctr = d["ctr"]
    peer_avg_rev = d["peer_avg_reviews"]
    merch_reviews = merchant.get("performance", {}).get("reviews", 0) or merchant.get("reviews_count", 0)
    cat_scope = peer.get("scope", "peers in your area")
    if peer_ctr > 0:
        ctr_pct_vs_peers = round((merch_ctr - peer_ctr) / peer_ctr * 100)
        if ctr_pct_vs_peers <= -20:
            d["social_proof_metric"] = (
                f"CTR {abs(ctr_pct_vs_peers)}% below {cat_scope} median "
                f"({merch_ctr:.3f} vs {peer_ctr:.3f}) — bottom tier"
            )
        elif ctr_pct_vs_peers >= 20:
            d["social_proof_metric"] = (
                f"CTR {ctr_pct_vs_peers}% above {cat_scope} median — top tier"
            )
        else:
            d["social_proof_metric"] = (
                f"CTR near {cat_scope} median ({merch_ctr:.3f} vs {peer_ctr:.3f})"
            )
    else:
        d["social_proof_metric"] = "peer CTR data unavailable"

    # Review themes
    d["review_themes"] = merchant.get("review_themes", [])
    d["top_pos_review"] = next((r for r in d["review_themes"] if r.get("sentiment") == "pos"), {})
    d["top_neg_review"] = next((r for r in d["review_themes"] if r.get("sentiment") == "neg"), {})

    # Conversation history
    conv = merchant.get("conversation_history", [])
    d["last_vera_touch_days"] = None
    d["last_merchant_reply"] = None
    if conv:
        try:
            last = conv[-1]
            if last.get("ts"):
                dt = datetime.fromisoformat(last["ts"].replace("Z", "+00:00"))
                d["last_vera_touch_days"] = (datetime.now(timezone.utc) - dt).days
            for turn in reversed(conv):
                if turn.get("from") == "merchant":
                    d["last_merchant_reply"] = turn.get("body", "")[:200]
                    break
        except Exception:
            pass
    d["conv_history_snippet"] = conv[-3:] if conv else []

    # Trigger payload analysis
    trg_payload = trigger.get("payload", {})
    trg_kind = trigger.get("kind", "")

    # Digest item resolution
    d["digest_item"] = None
    top_item_id = trg_payload.get("top_item_id") or trg_payload.get("digest_item_id")
    if top_item_id:
        for item in category.get("digest", []):
            if item.get("id") == top_item_id:
                d["digest_item"] = item
                break

    # Computed trigger-specific values
    if trg_kind == "perf_dip":
        metric = trg_payload.get("metric", "views")
        delta_pct = trg_payload.get("delta_pct", 0)
        baseline = trg_payload.get("vs_baseline", 0)
        d["dip_metric"] = metric
        d["dip_pct"] = abs(int(delta_pct * 100))
        d["dip_baseline"] = baseline
        d["dip_window"] = trg_payload.get("window", "7d")

    elif trg_kind in ("perf_spike", "seasonal_perf_dip"):
        metric = trg_payload.get("metric", "views")
        delta_pct = trg_payload.get("delta_pct", 0)
        d["spike_metric"] = metric
        d["spike_pct"] = abs(int(delta_pct * 100))
        d["spike_direction"] = "up" if delta_pct > 0 else "down"
        d["spike_is_seasonal"] = trg_payload.get("is_expected_seasonal", False)
        d["spike_season_note"] = trg_payload.get("season_note", "")

    elif trg_kind == "recall_due":
        d["recall_service"] = trg_payload.get("service_due", "").replace("_", " ")
        d["recall_last_date"] = trg_payload.get("last_service_date", "")
        d["recall_due_date"] = trg_payload.get("due_date", "")
        d["recall_slots"] = trg_payload.get("available_slots", [])
        months = months_since(d["recall_last_date"]) if d["recall_last_date"] else None
        d["recall_months_since"] = months

    elif trg_kind == "chronic_refill_due":
        d["refill_molecules"] = trg_payload.get("molecule_list", [])
        d["refill_runs_out"] = trg_payload.get("stock_runs_out_iso", "")[:10]
        d["delivery_address_saved"] = trg_payload.get("delivery_address_saved", False)

    elif trg_kind == "competitor_opened":
        d["comp_name"] = trg_payload.get("competitor_name", "")
        d["comp_distance_km"] = trg_payload.get("distance_km", "")
        d["comp_offer"] = trg_payload.get("their_offer", "")
        d["comp_opened"] = trg_payload.get("opened_date", "")

    elif trg_kind in ("festival_upcoming", "ipl_match_today"):
        d["event_name"] = trg_payload.get("festival") or trg_payload.get("match", "")
        d["event_date"] = trg_payload.get("date", "")
        d["event_days_until"] = trg_payload.get("days_until", days_until(d.get("event_date", "")))
        d["is_weeknight"] = trg_payload.get("is_weeknight", True)
        d["match_time"] = trg_payload.get("match_time_iso", "")[:16].replace("T", " ")
        d["venue"] = trg_payload.get("venue", "")

    elif trg_kind == "renewal_due":
        d["renewal_days"] = trg_payload.get("days_remaining", d["sub_days_left"])
        d["renewal_amount"] = trg_payload.get("renewal_amount", "")

    elif trg_kind == "milestone_reached":
        d["milestone_metric"] = trg_payload.get("metric", "")
        d["milestone_value"] = trg_payload.get("value_now", 0)
        d["milestone_target"] = trg_payload.get("milestone_value", 0)
        d["milestone_imminent"] = trg_payload.get("is_imminent", False)

    elif trg_kind == "review_theme_emerged":
        d["review_theme_name"] = trg_payload.get("theme", "")
        d["review_theme_count"] = trg_payload.get("occurrences_30d", 0)
        d["review_trend"] = trg_payload.get("trend", "")
        d["review_quote"] = trg_payload.get("common_quote", "")

    elif trg_kind == "supply_alert":
        d["alert_molecule"] = trg_payload.get("molecule", "")
        d["alert_batches"] = trg_payload.get("affected_batches", [])
        d["alert_manufacturer"] = trg_payload.get("manufacturer", "")

    elif trg_kind == "winback_eligible":
        d["winback_days_since_expiry"] = trg_payload.get("days_since_expiry", d["sub_days_since_expiry"])
        d["winback_lapsed_since"] = trg_payload.get("lapsed_customers_added_since_expiry", 0)
        d["winback_perf_dip"] = abs(int(trg_payload.get("perf_dip_pct", 0) * 100))

    elif trg_kind == "gbp_unverified":
        d["gbp_uplift_pct"] = int(trg_payload.get("estimated_uplift_pct", 0) * 100)
        d["gbp_verify_path"] = trg_payload.get("verification_path", "")

    elif trg_kind == "cde_opportunity":
        dig = d["digest_item"] or {}
        d["cde_credits"] = trg_payload.get("credits", dig.get("credits", 0))
        d["cde_fee"] = trg_payload.get("fee", dig.get("actionable", ""))
        d["cde_date"] = dig.get("date", "")[:10] if dig.get("date") else ""

    elif trg_kind in ("customer_lapsed_hard", "customer_lapsed_soft"):
        d["lapse_days"] = trg_payload.get("days_since_last_visit", 0)
        d["lapse_weeks"] = d["lapse_days"] // 7
        d["lapse_focus"] = trg_payload.get("previous_focus", "")
        d["lapse_months_member"] = trg_payload.get("previous_membership_months", 0)

    elif trg_kind == "trial_followup":
        d["trial_date"] = trg_payload.get("trial_date", "")
        d["trial_slots"] = trg_payload.get("next_session_options", [])

    elif trg_kind == "wedding_package_followup":
        d["wedding_date"] = trg_payload.get("wedding_date", "")
        d["wedding_days_until"] = trg_payload.get("days_to_wedding") or days_until(d.get("wedding_date", ""))
        d["wedding_trial_done"] = trg_payload.get("trial_completed", "")
        d["wedding_next_window"] = trg_payload.get("next_step_window_open", "")

    elif trg_kind == "category_seasonal":
        d["seasonal_trends"] = trg_payload.get("trends", [])

    # Customer-derived values
    if customer:
        cid = customer.get("identity", {})
        rel = customer.get("relationship", {})
        prefs = customer.get("preferences", {})
        d["cust_name"] = cid.get("name", "Customer")
        d["cust_lang"] = cid.get("language_pref", "en")
        d["cust_age_band"] = cid.get("age_band", "")
        d["cust_state"] = customer.get("state", "")
        d["cust_visits"] = rel.get("visits_total", 0)
        d["cust_last_visit"] = rel.get("last_visit", "")
        d["cust_months_since"] = months_since(d["cust_last_visit"]) if d["cust_last_visit"] else None
        d["cust_services"] = rel.get("services_received", [])
        d["cust_lifetime_value"] = rel.get("lifetime_value", 0)
        d["cust_preferred_slot"] = prefs.get("preferred_slots", "")
        d["cust_channel"] = prefs.get("channel", "whatsapp")

    return d


# ── TRIGGER-SPECIFIC CONTEXT SLICE ────────────────────────────────────────────
def build_primary_signal(trg_kind: str, d: dict, trigger: dict, category: dict) -> str:
    """
    Return a tight 2-3 line description of the SINGLE primary signal to build
    the message around. This is the key differentiator — choose the sharpest hook.
    """
    p = trigger.get("payload", {})

    if trg_kind == "research_digest":
        item = d.get("digest_item", {}) or {}
        return (
            f"PRIMARY SIGNAL: New research — {item.get('title', 'research item')}\n"
            f"  Source: {item.get('source', 'journal')}\n"
            f"  Key stat: trial_n={item.get('trial_n','?')}, segment={item.get('patient_segment','?')}\n"
            f"  Summary: {item.get('summary', '')[:200]}\n"
            f"  Actionable: {item.get('actionable', '')}\n"
            f"  Merchant anchor: {d['owner_first']} has {d['high_risk_adults']} high-risk adult patients"
            if d.get("high_risk_adults") else
            f"  Merchant anchor: {d['active_offer_titles'][0] if d['active_offer_titles'] else 'no active offers'}"
        )

    elif trg_kind == "regulation_change":
        item = d.get("digest_item", {}) or {}
        return (
            f"PRIMARY SIGNAL: Compliance deadline — {item.get('title', 'regulation change')}\n"
            f"  Source: {item.get('source', 'authority')}\n"
            f"  Deadline: {trigger.get('payload', {}).get('deadline_iso', '')[:10]}\n"
            f"  What changes: {item.get('summary', '')[:250]}\n"
            f"  What merchant must do: {item.get('actionable', '')}"
        )

    elif trg_kind == "perf_dip":
        return (
            f"PRIMARY SIGNAL: Performance dip — {d['dip_metric']} down {d['dip_pct']}% "
            f"in last {d['dip_window']} (baseline was {d['dip_baseline']})\n"
            f"  CTR: {d['ctr']:.3f} vs peer median {d['peer_ctr']:.3f} "
            f"({d['ctr_vs_peer_label']} by {abs(d['ctr_vs_peer_pct'])}%)\n"
            f"  Active offers: {d['active_offer_titles'] or 'none — suggest from catalog'}"
        )

    elif trg_kind in ("seasonal_perf_dip", "perf_spike"):
        direction = "dip" if d.get("spike_direction") == "down" else "spike"
        seasonal_note = f"  This is EXPECTED seasonal {direction}: {d.get('spike_season_note','')}" \
                        if d.get("spike_is_seasonal") else "  This is NOT seasonal — flag as anomaly."
        return (
            f"PRIMARY SIGNAL: {d.get('spike_metric','views')} {direction} {d.get('spike_pct',0)}% "
            f"this week\n{seasonal_note}\n"
            f"  Active members/customers: {d.get('active_members') or d.get('total_customers_ytd', '?')}"
        )

    elif trg_kind == "recall_due":
        slots_text = "; ".join(s.get("label", "") for s in (d.get("recall_slots") or [])[:2])
        return (
            f"PRIMARY SIGNAL: {d['cust_name']}'s {d.get('recall_service','cleaning')} recall is due\n"
            f"  Months since last visit: {d.get('recall_months_since', '?')}\n"
            f"  Available slots: {slots_text or 'check schedule'}\n"
            f"  Merchant's active offer: {d['active_offer_titles'][0] if d['active_offer_titles'] else 'none'}\n"
            f"  Customer language: {d.get('cust_lang','en')} | preferred: {d.get('cust_preferred_slot','any')}"
        )

    elif trg_kind == "chronic_refill_due":
        mols = ", ".join(d.get("refill_molecules", []))
        return (
            f"PRIMARY SIGNAL: {d.get('cust_name','Customer')}'s medicines running out {d.get('refill_runs_out','soon')}\n"
            f"  Molecules: {mols}\n"
            f"  Home delivery: {'available (address saved)' if d.get('delivery_address_saved') else 'ask for address'}\n"
            f"  Merchant's active offers: {', '.join(d['active_offer_titles']) or 'Free Home Delivery >₹499, Senior 15% OFF'}"
        )

    elif trg_kind == "ipl_match_today":
        # The KEY judgment: weeknight vs weekend changes strategy completely
        if d.get("is_weeknight"):
            judgment = "JUDGMENT: Weeknight IPL → higher footfall likely → push dine-in/BOGO promo."
        else:
            judgment = "JUDGMENT: Weekend IPL → people watch at home, restaurant covers typically -12%. " \
                       "Recommend delivery-only push instead of dine-in promo. This is contrarian but correct."
        return (
            f"PRIMARY SIGNAL: IPL match today — {d.get('event_name','match')} at {d.get('venue','stadium')}, {d.get('match_time','7:30pm')}\n"
            f"  Is weeknight: {d.get('is_weeknight', True)}\n"
            f"  {judgment}\n"
            f"  Merchant's active offers: {', '.join(d['active_offer_titles']) or 'none'}"
        )

    elif trg_kind == "festival_upcoming":
        return (
            f"PRIMARY SIGNAL: {d.get('event_name','festival')} is {d.get('event_days_until','?')} days away\n"
            f"  Category relevance: {p.get('category_relevance', [d['cat_slug']])}\n"
            f"  Merchant's active offers: {', '.join(d['active_offer_titles']) or 'suggest from catalog'}\n"
            f"  Suggest a festival-themed campaign or GBP post."
        )

    elif trg_kind == "competitor_opened":
        merchant_adv = ""
        if d["active_offer_titles"]:
            merchant_adv = f"Merchant's offer: {d['active_offer_titles'][0]}"
        if d.get("top_pos_review"):
            merchant_adv += f" | Top review theme: {d['top_pos_review'].get('theme','')} ({d['top_pos_review'].get('common_quote','')})"
        return (
            f"PRIMARY SIGNAL: {d.get('comp_name','Competitor')} opened {d.get('comp_distance_km','?')}km away "
            f"on {d.get('comp_opened','?')}\n"
            f"  Their offer: {d.get('comp_offer','unknown')}\n"
            f"  {merchant_adv}\n"
            f"  Merchant advantage: established {d['established_year']}, {d['total_customers_ytd']} customers YTD\n"
            f"  Lapsed customers to reactivate: {d['lapsed_180d'] or d['lapsed_90d']}"
        )

    elif trg_kind == "renewal_due":
        return (
            f"PRIMARY SIGNAL: Subscription expires in {d.get('renewal_days', d['sub_days_left'])} days "
            f"(Plan: {d['sub_plan']})\n"
            f"  Loss frame: GBP profile goes inactive, customer outreach pauses\n"
            f"  Renewal amount: ₹{d.get('renewal_amount', '')}\n"
            f"  Merchant's current CTR: {d['ctr']:.3f} | lapsed customers: {d['lapsed_180d'] or d['lapsed_90d']}"
        )

    elif trg_kind == "winback_eligible":
        return (
            f"PRIMARY SIGNAL: Subscription lapsed {d.get('winback_days_since_expiry','?')} days ago\n"
            f"  Performance dip since expiry: {d.get('winback_perf_dip','?')}%\n"
            f"  Customers lapsed since expiry: {d.get('winback_lapsed_since','?')}\n"
            f"  Frame: what they're LOSING (visibility, outreach), not what they're fixing."
        )

    elif trg_kind == "milestone_reached":
        return (
            f"PRIMARY SIGNAL: {d.get('milestone_metric','metric')} at {d.get('milestone_value','?')} "
            f"(approaching milestone of {d.get('milestone_target','?')})\n"
            f"  {'IMMINENT — they are about to hit it!' if d.get('milestone_imminent') else 'Recently crossed.'}\n"
            f"  Suggest: GBP post to celebrate, WhatsApp to top customers, or review request campaign."
        )

    elif trg_kind == "dormant_with_vera":
        days_dormant = p.get("days_since_last_merchant_message", d.get("last_vera_touch_days", "?"))
        last_topic = p.get("last_topic", "subscription")
        return (
            f"PRIMARY SIGNAL: Merchant hasn't replied to Vera in {days_dormant} days\n"
            f"  Last topic: {last_topic}\n"
            f"  Approach: re-engage with a useful, non-nagging update about their account\n"
            f"  Anchor on ONE real signal from their data: {d['signals'][:2] if d['signals'] else 'check offers/perf'}"
        )

    elif trg_kind == "review_theme_emerged":
        return (
            f"PRIMARY SIGNAL: Review theme '{d.get('review_theme_name','')}' "
            f"has {d.get('review_theme_count','?')} occurrences in 30d ({d.get('review_trend','')})\n"
            f"  Common quote: \"{d.get('review_quote','')}\"\n"
            f"  Offer to help: draft a response template, or suggest an operational fix."
        )

    elif trg_kind == "supply_alert":
        batches = ", ".join(d.get("alert_batches", []))
        # Estimate affected customers from chronic-Rx count
        affected_est = min(d["chronic_rx_count"], max(1, d["chronic_rx_count"] // 11)) if d["chronic_rx_count"] else "some"
        return (
            f"PRIMARY SIGNAL: Recall on {d.get('alert_molecule','molecule')} batches {batches} by {d.get('alert_manufacturer','Mfr')}\n"
            f"  Estimated affected chronic-Rx customers: ~{affected_est} of {d['chronic_rx_count']}\n"
            f"  Frame: sub-potency, no safety risk — but customers must be informed for replacement.\n"
            f"  Offer: draft WhatsApp note to affected customers + replacement-pickup workflow."
        )

    elif trg_kind == "gbp_unverified":
        return (
            f"PRIMARY SIGNAL: Google Business Profile is unverified\n"
            f"  Verification path: {d.get('gbp_verify_path','postcard or phone call')}\n"
            f"  Estimated uplift from verifying: {d.get('gbp_uplift_pct','?')}% more views\n"
            f"  At current views ({d['views_30d']}/month), that's ~{int(d['views_30d'] * (d.get('gbp_uplift_pct',30)/100))} extra views."
        )

    elif trg_kind == "cde_opportunity":
        dig = d.get("digest_item", {}) or {}
        return (
            f"PRIMARY SIGNAL: CDE/webinar opportunity — {dig.get('title','training')}\n"
            f"  Date: {d.get('cde_date','upcoming')} | Credits: {d.get('cde_credits','?')} | Fee: {d.get('cde_fee','see below')}\n"
            f"  Speaker/summary: {dig.get('summary','')[:200]}\n"
            f"  Position as peer value, not a sales pitch."
        )

    elif trg_kind in ("customer_lapsed_hard", "customer_lapsed_soft"):
        return (
            f"PRIMARY SIGNAL: {d.get('cust_name','Customer')} lapsed ({d.get('lapse_weeks','?')} weeks, {d.get('lapse_days','?')} days)\n"
            f"  Previous focus: {d.get('lapse_focus','general')}\n"
            f"  Was a member {d.get('lapse_months_member','?')} months\n"
            f"  Frame: no shame, no guilt. Offer something specific to their past goal.\n"
            f"  Active offer to attach: {d['active_offer_titles'][0] if d['active_offer_titles'] else 'suggest from catalog'}"
        )

    elif trg_kind == "trial_followup":
        slots = "; ".join(s.get("label","") for s in (d.get("trial_slots") or [])[:2])
        return (
            f"PRIMARY SIGNAL: {d.get('cust_name','Customer')} attended trial on {d.get('trial_date','?')}\n"
            f"  Next session options: {slots or 'check schedule'}\n"
            f"  Active offers: {d['active_offer_titles'][0] if d['active_offer_titles'] else 'First Month @ best price'}"
        )

    elif trg_kind == "wedding_package_followup":
        return (
            f"PRIMARY SIGNAL: {d.get('cust_name','Customer')}'s wedding is {d.get('wedding_days_until','?')} days away\n"
            f"  Trial done: {d.get('wedding_trial_done','?')}\n"
            f"  Next step window: {d.get('wedding_next_window','skin prep or main booking')}\n"
            f"  Frame urgency around the window, not generic sales."
        )

    elif trg_kind == "curious_ask_due":
        ask_templates = {
            "what_service_in_demand_this_week": "Ask: which service/dish/product has been most asked-for this week?",
        }
        ask_tmpl = p.get("ask_template", "")
        return (
            f"PRIMARY SIGNAL: Weekly curiosity check-in\n"
            f"  {ask_templates.get(ask_tmpl, 'Ask one genuine open-ended question about their business.')}\n"
            f"  Keep it conversational — one question, offer to do something with the answer.\n"
            f"  Last ask: {p.get('last_ask_at','never')}"
        )

    elif trg_kind == "active_planning_intent":
        last_msg = p.get("merchant_last_message", d.get("last_merchant_reply", ""))
        intent_topic = p.get("intent_topic", "")
        return (
            f"PRIMARY SIGNAL: Merchant committed to action — {intent_topic}\n"
            f"  Merchant said: \"{last_msg[:200]}\"\n"
            f"  CRITICAL: Do NOT ask more qualifying questions. EXECUTE.\n"
            f"  Deliver the plan/draft/next concrete step immediately.\n"
            f"  End with: 'Reply CONFIRM to proceed' or similar single-action CTA."
        )

    elif trg_kind == "category_seasonal":
        top_trends = "; ".join(d.get("seasonal_trends", [])[:3])
        return (
            f"PRIMARY SIGNAL: Seasonal demand shift\n"
            f"  Top trends: {top_trends}\n"
            f"  Suggest shelf/campaign action based on these shifts."
        )

    elif trg_kind == "appointment_tomorrow":
        return (
            f"PRIMARY SIGNAL: {d.get('cust_name','Customer')} has an appointment tomorrow\n"
            f"  Send a friendly reminder on behalf of the merchant.\n"
            f"  Brief, warm, no-CTA (confirmation is implicit)."
        )

    else:
        # Generic fallback
        return (
            f"PRIMARY SIGNAL: {trg_kind} trigger for {d['owner_first']}\n"
            f"  Active offers: {', '.join(d['active_offer_titles']) or 'none'}\n"
            f"  Signals: {', '.join(d['signals'][:3])}"
        )


# ── SYSTEM PROMPT ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Vera, magicpin's AI merchant assistant. You compose WhatsApp messages for Indian merchants.

DECISION QUALITY — what separates 50/50 from 30/50:
• Pick ONE primary signal. Do not list multiple facts and ask about all of them.
• Add judgment: if the data implies a counterintuitive recommendation, make it.
• Numbers from context only — never invented. If you don't have it, don't use it.
• CRITICAL: The first sentence of the message must contain the data hook (the number, finding, or event). Not the name. Not a warm-up. The hook.

MESSAGE LENGTH — strict:
• Maximum 55 words. Count them. WhatsApp messages over 55 words get ignored.
• 2-3 sentences max. One hook sentence + one context sentence + one CTA sentence.
• If you exceed 55 words, rewrite. No exceptions.

MESSAGE CRAFT:
• Address by first name — but AFTER the hook, or at the very start if needed for flow
• No preambles ("I hope this finds you well", "I wanted to reach out", "Just checking in")
• No self-re-introduction after turn 1
• No URLs in the message body
• No fake data, no invented stats, no competitor names you weren't given
• CTA must be a concrete ask: end with "Reply YES" / "Reply YES ya STOP" / "Slot A ya B" — never a vague question

LANGUAGE:
• Hindi-English code-mix (Hinglish) when merchant language includes "hi"
• Full Hindi if merchant speaks only Hindi
• Regional touches for Mumbai (Marathi), Chennai (Tamil), Hyderabad (Telugu), Bangalore (Kannada)
• Default: natural English

CATEGORY VOICE — non-negotiable:
• dentists: peer-clinical, source citations, technical vocabulary (fluoride varnish, caries, recall)
• salons: warm-operator, visual language, emojis OK sparingly
• restaurants: direct operator tone, uses "covers", "AOV", "delivery radius"
• gyms: coach energy but data-grounded, "conversion", "trial-to-paid", "ad spend"
• pharmacies: trustworthy-precise, calm, "molecule", "chronic-Rx", "sub-potency"

SEND_AS RULE:
• scope=customer → send_as=merchant_on_behalf (message goes from the shop to its customer)
• scope=merchant → send_as=vera (Vera talking to the shop owner)

OUTPUT — valid JSON only, no markdown:
{
  "body": "the WhatsApp message (≤70 words)",
  "cta": "open_ended" | "binary_yes_no" | "binary_confirm_cancel" | "multi_choice_slot" | "none",
  "send_as": "vera" | "merchant_on_behalf",
  "rationale": "Trigger: [kind] | Signal: [exact data point] | Decision: [what and why] | Lever: [psychological hook]"
}"""


def build_compose_prompt(d: dict, trigger: dict, category: dict, customer: Optional[dict]) -> str:
    """Build the tight, structured prompt for the LLM. Python does the math; LLM does the writing."""
    trg_kind = trigger.get("kind", "")
    cat_slug = d.get("cat_slug", "")
    vocab = CATEGORY_VOCAB.get(cat_slug, CATEGORY_VOCAB.get("restaurants", {}))

    # Language — sensitive to PRIMARY language (first in list)
    langs = d.get("languages", ["en"])
    primary = str(langs[0]).lower() if langs else "en"
    langs_str = " ".join(str(l).lower() for l in langs)
    hi_primary = primary in ("hi", "hindi", "hi-en")
    lang_note = ""
    if hi_primary:
        lang_note = (
            "MANDATORY LANGUAGE: Hinglish (Hindi-English mix). NOT full English.\n"
            "  Examples: 'JIDA ka naya data hai — 38% better. Aapke 124 patients hain. Reply YES.'\n"
            "  'Competitor 1.3km pe khula. 78 lapsed patients reactivate karte hain? YES ya STOP?'"
        )
    elif "hi" in langs_str or "hindi" in langs_str:
        lang_note = "LANGUAGE: English preferred, Hinglish welcome (merchant is bilingual, English-first)."
    elif "ta" in langs_str:
        lang_note = "LANGUAGE: English with Tamil warmth."
    elif "te" in langs_str:
        lang_note = "LANGUAGE: English with Telugu warmth."
    elif "kn" in langs_str:
        lang_note = "LANGUAGE: English with Kannada warmth."
    elif "mr" in langs_str:
        lang_note = "LANGUAGE: English with Marathi warmth. 'Bhai/Tai' OK."
    else:
        lang_note = "LANGUAGE: Natural English."

    # Recent conversation context
    conv_lines = []
    for turn in d.get("conv_history_snippet", []):
        role = turn.get("from", "?")
        body = turn.get("body", "")[:150]
        conv_lines.append(f"  [{role}]: {body}")
    conv_section = "\nRECENT CONVERSATION (last 3 turns):\n" + "\n".join(conv_lines) if conv_lines else ""

    # Determine send_as from trigger scope
    trg_scope = trigger.get("scope", "merchant")
    send_as_directive = "merchant_on_behalf" if trg_scope == "customer" else "vera"

    # Customer block + persona injection for customer-scope
    cust_block = ""
    persona_block = ""
    if customer:
        cust_name = d.get("cust_name", "?")
        cust_first = cust_name.split()[0] if cust_name else "?"
        cust_lang = d.get("cust_lang", "en")
        cust_block = (
            f"CUSTOMER: {cust_name} | "
            f"visits={d.get('cust_visits',0)} | "
            f"months_since={d.get('cust_months_since','?')} | "
            f"lang={cust_lang} | "
            f"preferred_slot={d.get('cust_preferred_slot','any')} | "
            f"last_services={', '.join(d.get('cust_services',[])[:3])}"
        )
        if trg_scope == "customer":
            cust_lang_note = (
                "Write to them in Hinglish." if cust_lang in ("hi", "hi-en")
                else f"Customer language: {cust_lang}."
            )
            persona_block = (
                f"PERSONA: You ARE {d['owner_first']} from {d['merchant_name']} "
                f"personally WhatsApping {cust_first}.\n"
                f"  NOT Vera. NOT a bot. The actual clinic/shop owner texting their regular customer.\n"
                f"  Use first name ({cust_first}). Warm, personal, brief. {cust_lang_note}"
            )

    # Build prompt — language + persona FIRST (LLM pays most attention to beginning)
    lines = [lang_note, ""]
    if persona_block:
        lines += [persona_block, ""]
    lines += [
        f"TRIGGER: {trg_kind} | scope={trg_scope} | urgency={trigger.get('urgency',2)}/5 | send_as={send_as_directive}",
        "",
        build_primary_signal(trg_kind, d, trigger, category),
        "",
        f"MERCHANT: {d['merchant_name']} | {d['owner_first']} | {d['locality']}, {d['city']} | Est. {d['established_year']}",
        f"  Subscription: {d['sub_status']} ({d['sub_plan']}, {d['sub_days_left']} days left)",
        f"  CTR: {d['ctr']:.3f} vs peer {d['peer_ctr']:.3f} ({d['ctr_vs_peer_label']} by {abs(d['ctr_vs_peer_pct'])}%) | "
        f"Views 7d: {pct(d['views_7d_delta'])} | Calls 7d: {pct(d['calls_7d_delta'])}",
        f"  Social Proof: {d.get('social_proof_metric', 'N/A')}",
        f"  Active offers: {', '.join(d['active_offer_titles']) or 'NONE'}",
        f"  Category offer catalog: {', '.join(d['cat_offers'])}",
        f"  Signals: {', '.join(d['signals'][:3]) if d['signals'] else 'none'}",
        f"  Peer stats: CTR peer median {d['peer_ctr']:.3f} | avg reviews {d['peer_avg_reviews']} | avg views/mo {d['peer_avg_views']}",
    ]

    if cust_block:
        lines.append(cust_block)

    if conv_section:
        lines.append(conv_section)

    lines += [
        f"\nCATEGORY: {cat_slug} | Voice: {vocab.get('tone','')}",
        f"  Domain words: {vocab.get('domain_words','')}",
        f"  Avoid: {vocab.get('avoid','')}",
        f"\n{lang_note}",
        "\n⚠️  MESSAGE LIMIT: 70 words MAX. Count before finalising. Trim ruthlessly.",
        "⚠️  First sentence = the data hook. Not the name. Not a greeting.",
        f"⚠️  send_as must be '{send_as_directive}' (set by trigger scope).",
        "\nRespond ONLY with valid JSON. No markdown, no explanation outside the JSON.",
    ]
    return "\n".join(l for l in lines if l is not None)


def build_reply_prompt(
    d: dict, category: dict, customer: Optional[dict],
    conv_history: list[dict], incoming_msg: str, turn: int, auto_count: int
) -> str:
    """Tight reply prompt — focus on what to do NEXT, not restating all context."""
    cat_slug = d.get("cat_slug", "")
    vocab = CATEGORY_VOCAB.get(cat_slug, {})

    hist_lines = [f"  [{h.get('from','?')}]: {h.get('body',h.get('msg',''))[:180]}" for h in conv_history[-5:]]

    cust_line = ""
    if customer:
        cust_line = f"\nCUSTOMER: {d.get('cust_name','?')} | state={d.get('cust_state','')} | lang={d.get('cust_lang','en')}"

    lines = [
        f"MERCHANT: {d['merchant_name']} ({cat_slug}) | {d['owner_first']} | offers: {', '.join(d['active_offer_titles']) or 'none'}",
        f"TURN: {turn} | AUTO-REPLY STREAK: {auto_count}",
        cust_line,
        f"\nCONVERSATION:\n" + "\n".join(hist_lines),
        f"\nMERCHANT SAYS NOW: \"{incoming_msg}\"",
        "",
        "RESPONSE RULES (in priority order):",
        "1. YES/commit/confirm → STOP qualifying. Deliver the COMPLETE draft/plan/action in this reply. End: 'Reply CONFIRM to proceed.'",
        "2. Specific question → Answer it precisely with 1-2 facts from context, then continue thread.",
        "3. Opt-out/frustration → action=end, 1 warm closing line, no argument.",
        "4. Auto-reply detected → action=send, flag for owner gently, ask YES/STOP.",
        "5. Off-topic → 1-sentence decline, redirect.",
        "6. Otherwise → advance the conversation with ONE next step, be specific.",
        f"Category voice: {vocab.get('tone','')}",
        "Reply body: ≤55 words. No URLs.",
        "",
        'Output JSON: {"action":"send"|"wait"|"end","body":"...","cta":"...","wait_seconds":N,"rationale":"Signal: X | Decision: Y | Lever: Z"}',
        "body and cta required when action=send; wait_seconds required when action=wait",
    ]
    return "\n".join(l for l in lines if l is not None)


# ── LLM CALL ─────────────────────────────────────────────────────────────────
def call_llm(system: str, user: str, temp: float = 0.0, max_tok: int = 700) -> Optional[dict]:
    if not groq_client:
        return None
    try:
        resp = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user",   "content": user}],
            temperature=temp,
            max_tokens=max_tok,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        return json.loads(raw)
    except Exception as e:
        log.error(f"LLM error: {e}")
        return None


# ── FALLBACK (no LLM) ─────────────────────────────────────────────────────────
def fallback_compose(d: dict, trigger: dict, customer: Optional[dict]) -> dict:
    """Rule-based fallback. Uses real merchant data but no LLM."""
    kind = trigger.get("kind", "")
    name = d["owner_first"]
    trg_scope = trigger.get("scope", "merchant")
    send_as = "merchant_on_behalf" if trg_scope == "customer" else "vera"

    if kind == "research_digest":
        item = d.get("digest_item", {}) or {}
        body = (f"{name}, {item.get('source','research')} mein ek item aaya jo aapke "
                f"patients ke liye relevant hai. Want me to pull it + draft a patient message?")
        cta = "open_ended"
    elif kind == "perf_dip":
        body = (f"{name}, your {d.get('dip_metric','calls')} are down {d.get('dip_pct','?')}% "
                f"this week vs baseline of {d.get('dip_baseline','?')}. "
                f"Want me to check what's changed and suggest a fix?")
        cta = "binary_yes_no"
    elif kind == "renewal_due":
        body = (f"{name}, your {d['sub_plan']} plan expires in {d.get('renewal_days', d['sub_days_left'])} days. "
                f"Renewal keeps your GBP active and customer outreach running. Reply YES to renew.")
        cta = "binary_yes_no"
    elif kind == "recall_due" and customer:
        offer = d["active_offer_titles"][0] if d["active_offer_titles"] else "cleaning"
        body = (f"Hi {d.get('cust_name','')}, {d['merchant_name']} yahan. "
                f"Aapka {d.get('recall_service','cleaning')} recall due hai. "
                f"{offer}. Reply YES to book a slot.")
        cta = "binary_yes_no"
    elif kind == "competitor_opened":
        body = (f"{name}, {d.get('comp_name','Competitor')} opened {d.get('comp_distance_km','?')}km away "
                f"with offer '{d.get('comp_offer','?')}'. "
                f"Aapke {d['lapsed_180d'] or d['lapsed_90d']} lapsed customers reactivate karne chahein?")
        cta = "binary_yes_no"
    else:
        body = (f"{name}, quick update on your account — want me to walk through what's new?")
        cta = "open_ended"

    return {"body": body, "cta": cta, "send_as": send_as,
            "rationale": f"Fallback: {kind} trigger"}


# ── COMPOSE ORCHESTRATOR ───────────────────────────────────────────────────────
def compose(trigger_id: str, trigger: dict, merchant: dict, category: dict,
            customer: Optional[dict] = None) -> Optional[dict]:
    sup_key = trigger.get("suppression_key", "")
    if sup_key and sup_key in suppressed:
        log.info(f"Suppressed {trigger_id}")
        return None

    d = build_derived(merchant, category, trigger, customer)
    prompt = build_compose_prompt(d, trigger, category, customer)
    result = call_llm(SYSTEM_PROMPT, prompt) if groq_client else None

    if not result or not result.get("body"):
        log.warning(f"LLM gave no body for {trigger_id}, using fallback")
        result = fallback_compose(d, trigger, customer)

    if sup_key:
        suppressed.add(sup_key)

    return result


# ── ENDPOINTS ─────────────────────────────────────────────────────────────────
@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _) in contexts:
        if scope in counts:
            counts[scope] += 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START_TIME),
            "contexts_loaded": counts, "llm": GROQ_MODEL if USE_LLM else "fallback"}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Vera Bot — Decision-First Engine",
        "team_members": ["Kritansh Tank"],
        "model": GROQ_MODEL,
        "approach": (
            "Decision-first engine: Python pre-computes all derived signals (CTR vs peer, "
            "months since visit, estimated affected patients, competitor delta) before the LLM call. "
            "ONE primary signal is selected per trigger kind — LLM writes around that hook, not a data dump. "
            "25+ trigger kinds each get domain-specific context slices. "
            "Auto-reply detection, intent-transition routing (YES→execute immediately), "
            "suppression tracking, anti-repetition, and graceful exit on opt-out."
        ),
        "contact_email": "tankkritansh088@gmail.com",
        "version": "2.2.0",
        "submitted_at": "2026-09-02T07:00:00Z",
    }


@app.post("/v1/context")
async def push_context(body: ContextBody):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        from fastapi import Response
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "invalid_scope", "details": f"Unknown: {body.scope}"}
        )

    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    # Same version = idempotent no-op → 200 (brief §2.1: "re-posting same version is a no-op")
    if cur and cur["version"] == body.version:
        return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}",
                "stored_at": now_iso()}
    # Strictly lower version = stale → 409
    if cur and cur["version"] > body.version:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=409,
            content={"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
        )

    contexts[key] = {"version": body.version, "payload": body.payload}
    log.info(f"Stored {body.scope}/{body.context_id} v{body.version}")
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}",
            "stored_at": now_iso()}


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    TICK_BUDGET = 25.0  # return early if approaching 30s judge timeout
    tick_start = time.time()

    for trg_id in body.available_triggers:
        trg_entry = contexts.get(("trigger", trg_id))
        if not trg_entry:
            log.warning(f"Trigger {trg_id} not in store")
            continue

        trigger  = trg_entry["payload"]
        merchant_id = trigger.get("merchant_id")
        customer_id = trigger.get("customer_id")
        if not merchant_id:
            continue

        m_entry = contexts.get(("merchant", merchant_id))
        if not m_entry:
            log.warning(f"Merchant {merchant_id} not in store")
            continue
        merchant = m_entry["payload"]

        cat_slug  = merchant.get("category_slug", "")
        c_entry   = contexts.get(("category", cat_slug))
        category  = c_entry["payload"] if c_entry else {}

        customer = None
        if customer_id:
            ce = contexts.get(("customer", customer_id))
            customer = ce["payload"] if ce else None

        sup_key  = trigger.get("suppression_key", "")
        if sup_key and sup_key in suppressed:
            continue

        # Open challenge #5: stop after 3 consecutive unanswered nudges per merchant
        if unanswered_sends.get(merchant_id, 0) >= 3:
            log.info(f"Skipping {merchant_id}: {unanswered_sends[merchant_id]} unanswered nudges")
            continue

        conv_id = f"conv_{merchant_id}_{trg_id}"
        if conv_id in ended_convs:
            continue
        if any(a["conversation_id"] == conv_id for a in actions):
            continue

        log.info(f"Composing {trigger.get('kind')} for {merchant_id}")
        composed = compose(trg_id, trigger, merchant, category, customer)
        if not composed:
            continue

        body_text = composed.get("body", "")
        if not body_text:
            continue

        # Anti-repetition
        if last_sent.get(conv_id) == body_text:
            log.warning(f"Anti-repetition skip: {conv_id}")
            continue
        last_sent[conv_id] = body_text
        unanswered_sends[merchant_id] = unanswered_sends.get(merchant_id, 0) + 1

        conversations.setdefault(conv_id, []).append(
            {"from": "vera", "body": body_text, "ts": body.now})

        owner_first = merchant.get("identity", {}).get("owner_first_name",
                                   merchant.get("identity", {}).get("name", "Merchant"))
        trg_kind = trigger.get("kind", "update")
        send_as = composed.get("send_as", "vera")

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": send_as,
            "trigger_id": trg_id,
            "template_name": f"{'merchant' if send_as == 'merchant_on_behalf' else 'vera'}_{trg_kind}_v2",
            "template_params": [owner_first, trg_kind, body_text[:80]],
            "body": body_text,
            "cta": composed.get("cta", "open_ended"),
            "suppression_key": sup_key,
            "rationale": composed.get("rationale", "Composed from 4-context signal selection"),
        })

        if len(actions) >= 20:
            break
        if time.time() - tick_start >= TICK_BUDGET:
            log.warning(f"Tick budget exhausted after {len(actions)} actions — returning early")
            break

    log.info(f"Tick {body.now}: {len(actions)} actions")
    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conv_id = body.conversation_id
    message = body.message.strip()
    turn = body.turn_number
    from_role = body.from_role

    if conv_id in ended_convs:
        return {"action": "end", "rationale": "Conversation already ended."}

    # Real merchant reply resets unanswered nudge counter
    if from_role == "merchant" and not is_auto_reply(message):
        unanswered_sends[body.merchant_id] = 0

    # Track turn in conversation
    conversations.setdefault(conv_id, []).append(
        {"from": from_role, "body": message, "ts": body.received_at})
    conv_history = conversations[conv_id]

    merchant = None; category = None; customer = None
    if body.merchant_id:
        me = contexts.get(("merchant", body.merchant_id))
        merchant = me["payload"] if me else None
        if merchant:
            ce = contexts.get(("category", merchant.get("category_slug", "")))
            category = ce["payload"] if ce else None
    if body.customer_id:
        cue = contexts.get(("customer", body.customer_id))
        customer = cue["payload"] if cue else None

    auto_count = auto_reply_counts.get(conv_id, 0)

    # ── Fast rule-based detection ──────────────────────────────────────────────
    if is_opt_out(message):
        ended_convs.add(conv_id)
        return {"action": "end",
                "rationale": "Merchant explicitly opted out. Closing + suppressing."}

    if is_auto_reply(message):
        new_c = auto_count + 1
        auto_reply_counts[conv_id] = new_c
        if new_c == 1:
            return {"action": "send",
                    "body": "Lagta hai auto-reply aa gaya 🙂 Jab owner dekhein, bas reply karein: YES ya NO.",
                    "cta": "binary_yes_no",
                    "rationale": "First auto-reply detected; one last flag for the owner then backing off."}
        else:
            # 2nd+ auto-reply → exit gracefully per Pattern B
            ended_convs.add(conv_id)
            return {"action": "end",
                    "rationale": f"Auto-reply {new_c}x — no real engagement. Closing gracefully."}

    # ── LLM reply composer ─────────────────────────────────────────────────────
    result = None
    if groq_client and merchant and category:
        d = build_derived(merchant, category, {}, customer)
        prompt = build_reply_prompt(d, category, customer, conv_history, message, turn, auto_count)
        raw = call_llm(
            """You are Vera, magicpin's WhatsApp AI for merchants. Respond to the merchant's latest message.
Keep it tight. One action, one clear next step. No URLs. Category-appropriate voice.
JSON output only: {"action":"send"|"wait"|"end","body":"...","cta":"...","wait_seconds":N,"rationale":"..."}""",
            prompt, temp=0.1, max_tok=500
        )
        if raw and raw.get("action") in ("send", "wait", "end"):
            result = raw

    if not result:
        # Safe default when LLM unavailable
        if is_explicit_yes(message):
            result = {"action": "send",
                      "body": "Bilkul! Let me get that done now. Give me a moment.",
                      "cta": "none",
                      "rationale": "Merchant affirmed; executing."}
        else:
            result = {"action": "send",
                      "body": "Got it — let me look into that and get back to you shortly.",
                      "cta": "open_ended",
                      "rationale": "Default acknowledgment"}

    action = result.get("action", "send")

    if action == "end":
        ended_convs.add(conv_id)
        return {"action": "end", "rationale": result.get("rationale", "Ended.")}

    if action == "wait":
        return {"action": "wait",
                "wait_seconds": result.get("wait_seconds", 3600),
                "rationale": result.get("rationale", "Backing off.")}

    body_text = result.get("body", "")
    # Anti-repetition
    if last_sent.get(conv_id) == body_text:
        body_text = body_text.rstrip("।.") + " — kuch aur help chahiye?"

    last_sent[conv_id] = body_text
    conversations[conv_id].append({"from": "vera", "body": body_text, "ts": now_iso()})

    log.info(f"Reply conv={conv_id} turn={turn} action=send")
    return {"action": "send", "body": body_text,
            "cta": result.get("cta", "open_ended"),
            "rationale": result.get("rationale", "continued")}


@app.post("/v1/teardown")
async def teardown():
    for s in [contexts, conversations, suppressed, ended_convs, auto_reply_counts, last_sent, unanswered_sends]:
        s.clear()
    return {"status": "wiped"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    log.info(f"Vera Bot v2 | port={port} | LLM={'Groq:' + GROQ_MODEL if USE_LLM else 'FALLBACK'}")
    uvicorn.run("bot:app", host="0.0.0.0", port=port, reload=False)
