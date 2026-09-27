"""
Browser adapter for the demo site (runs inside Pyodide).

Mirrors what bot.py's /v1/tick and /v1/reply do, using the exact same composer and
conversation handler, but without FastAPI so it can run as a static GitHub Pages app.
"""

from __future__ import annotations

import json

from composer import compose, validate
from conversation_handlers import new_state, respond
from dataset_loader import load_all

DATA = load_all()
CONVS: dict[str, dict] = {}
INBOUND: dict[str, list[str]] = {}


def _brief_merchant(m: dict) -> dict:
    ident, perf = m.get("identity", {}), m.get("performance", {})
    return {
        "id": m["merchant_id"], "name": ident.get("name"), "owner": ident.get("owner_first_name"),
        "category": m.get("category_slug"), "city": ident.get("city"), "locality": ident.get("locality"),
        "languages": ident.get("languages"), "verified": ident.get("verified"),
        "subscription": m.get("subscription"), "views": perf.get("views"), "calls": perf.get("calls"),
        "ctr": perf.get("ctr"), "delta_7d": perf.get("delta_7d"),
        "offers": [o.get("title") for o in m.get("offers", []) if o.get("status") == "active"],
        "signals": m.get("signals", []), "customer_aggregate": m.get("customer_aggregate", {}),
        "review_themes": m.get("review_themes", []),
    }


def catalog() -> str:
    pairs = {p["trigger_id"]: p["test_id"] for p in DATA["test_pairs"]}
    triggers = []
    for tid, t in DATA["triggers"].items():
        m = DATA["merchants"][t["merchant_id"]]
        cu = DATA["customers"].get(t.get("customer_id")) if t.get("customer_id") else None
        triggers.append({
            "id": tid, "kind": t["kind"], "urgency": t.get("urgency"), "scope": t.get("scope"),
            "merchant_id": t["merchant_id"], "merchant_name": m["identity"]["name"], "category": m["category_slug"],
            "city": m["identity"].get("city"), "customer_name": (cu or {}).get("identity", {}).get("name"),
            "placeholder": bool(t.get("payload", {}).get("placeholder")), "test_id": pairs.get(tid),
        })
    triggers.sort(key=lambda x: (x["test_id"] is None, x["test_id"] or "", x["id"]))
    return json.dumps({"triggers": triggers, "counts": {k: len(DATA[k]) for k in ("categories", "merchants", "customers", "triggers")}})


def start(trigger_id: str) -> str:
    t = DATA["triggers"][trigger_id]
    m = DATA["merchants"][t["merchant_id"]]
    cu = DATA["customers"].get(t.get("customer_id")) if t.get("customer_id") else None
    cat = DATA["categories"][m["category_slug"]]
    msg = compose(cat, m, t, cu)
    conv_id = f"conv_{trigger_id}_{len(CONVS) + 1}"
    state = new_state(conv_id, m["merchant_id"], (cu or {}).get("customer_id"), t, msg)
    state["trigger_payload"] = t.get("payload") or {}
    CONVS[conv_id] = state
    return json.dumps({
        "conversation_id": conv_id,
        "message": {k: msg[k] for k in ("body", "cta", "send_as", "suppression_key", "rationale", "template_name", "template_params")},
        "problems": validate(msg, cat),
        "merchant": _brief_merchant(m),
        "customer": cu,
        "trigger": t,
        "peer_stats": cat.get("peer_stats", {}),
    }, ensure_ascii=False)


def reply(conv_id: str, text: str) -> str:
    state = CONVS[conv_id]
    t = DATA["triggers"].get(state.get("trigger_id")) or {}
    m = DATA["merchants"].get(state["merchant_id"]) or {}
    role = "customer" if state.get("send_as") == "merchant_on_behalf" else "merchant"
    key = f"{state['merchant_id']}:{role}"
    state.update(merchant=m, category=DATA["categories"].get(m.get("category_slug"), {}),
                 customer=DATA["customers"].get(state.get("customer_id")), from_role=role,
                 merchant_inbound=INBOUND.get(key, []))
    result = respond(state, text)
    INBOUND.setdefault(key, []).append(text)
    for k in ("merchant", "category", "customer", "merchant_inbound"):
        state.pop(k, None)
    result["label"] = state.get("last_label")
    return json.dumps(result, ensure_ascii=False)


def submission() -> str:
    out = []
    for p in DATA["test_pairs"]:
        t = DATA["triggers"][p["trigger_id"]]
        m = DATA["merchants"][p["merchant_id"]]
        cu = DATA["customers"].get(p["customer_id"]) if p.get("customer_id") else None
        msg = compose(DATA["categories"][m["category_slug"]], m, t, cu)
        out.append({"test_id": p["test_id"], "trigger_id": p["trigger_id"], "kind": t["kind"],
                    "merchant": m["identity"]["name"], "category": m["category_slug"], "body": msg["body"],
                    "cta": msg["cta"], "send_as": msg["send_as"], "rationale": msg["rationale"]})
    return json.dumps(out, ensure_ascii=False)
