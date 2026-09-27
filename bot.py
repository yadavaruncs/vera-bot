"""
Vera challenge bot.

  * compose(category, merchant, trigger, customer) -> dict      (the §7.1 contract)
  * FastAPI app exposing /v1/context, /v1/tick, /v1/reply, /v1/healthz, /v1/metadata, /v1/teardown

Run:  uvicorn bot:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import composer
from conversation_handlers import new_state, respond

VALID_SCOPES = ("category", "merchant", "customer", "trigger")
MAX_ACTIONS_PER_TICK = 20
MAX_PER_RECIPIENT_PER_TICK = 2      # a merchant can get at most 2 distinct-trigger messages in one tick
OPT_OUT_DAYS = 30


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    """Challenge contract: body, cta, send_as, suppression_key, rationale (+ template fields)."""
    out = composer.compose(category, merchant, trigger, customer)
    for internal in ("followup", "composer_version", "lang"):
        out.pop(internal, None)
    return out


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        self.contexts: dict[tuple[str, str], dict] = {}     # (scope, id) -> {"version", "payload"}
        self.conversations: dict[str, dict] = {}
        self.sent_keys: set[str] = set()                    # suppression keys already used
        self.merchant_inbound: dict[str, list[str]] = {}    # for cross-conversation auto-reply detection
        self.merchant_blocked_until: dict[str, datetime] = {}
        self.merchant_bodies: dict[str, set] = {}          # recipient -> bodies already sent
        self.conv_counter = 0

    def get(self, scope: str, cid: Optional[str]) -> Optional[dict]:
        if not cid:
            return None
        rec = self.contexts.get((scope, cid))
        return rec["payload"] if rec else None

    def category_for(self, merchant: Optional[dict], trigger: Optional[dict] = None) -> dict:
        slug = (merchant or {}).get("category_slug") or ((trigger or {}).get("payload") or {}).get("category")
        return self.get("category", slug) or {}


STORE = Store()
START = time.time()
app = FastAPI(title="Vera bot", version="2.0.0")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: Any) -> Optional[datetime]:
    d = composer.parse_dt(ts) if isinstance(ts, str) else None
    if d and d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/v1/healthz")
def healthz():
    counts = {s: 0 for s in VALID_SCOPES}
    with STORE.lock:
        for (scope, _cid) in STORE.contexts:
            counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


@app.get("/v1/metadata")
def metadata():
    return {
        "team_name": "Arun Yadav",
        "team_members": ["Arun Yadav"],
        "model": "deterministic grounded composer (optional claude-sonnet-5 for off-script Q&A)",
        "approach": ("Trigger-kind dispatch over 26 handlers; every fact pulled from the 4 contexts (no invention); "
                     "category voice + language routing; placeholder-payload fallbacks; multi-turn state machine with "
                     "auto-reply / opt-out / intent-handoff / off-topic detection."),
        "contact_email": "yadavaruncs@gmail.com",
        "version": "2.0.0",
        "submitted_at": "2026-09-27T00:00:00Z",
    }


@app.post("/v1/context")
async def push_context(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_json"})
    scope, cid, version, payload = body.get("scope"), body.get("context_id"), body.get("version"), body.get("payload")
    if scope not in VALID_SCOPES:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_scope",
                                                      "details": f"scope must be one of {VALID_SCOPES}"})
    if not cid or not isinstance(payload, dict) or not isinstance(version, int):
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "malformed",
                                                      "details": "context_id, integer version and object payload required"})
    with STORE.lock:
        cur = STORE.contexts.get((scope, cid))
        if cur and cur["version"] >= version:
            return JSONResponse(status_code=409, content={"accepted": False, "reason": "stale_version",
                                                          "current_version": cur["version"]})
        STORE.contexts[(scope, cid)] = {"version": version, "payload": payload}
    return {"accepted": True, "ack_id": f"ack_{cid}_v{version}", "stored_at": _now_utc().isoformat().replace("+00:00", "Z")}


def _conv_id(merchant_id: str, trigger: dict, customer_id: Optional[str]) -> str:
    kind = trigger.get("kind", "msg")
    who = (customer_id or merchant_id or "x")
    who = re.sub(r"^(c|m)_\d+_", "", who)[:24]
    base = f"conv_{who}_{kind}"
    STORE.conv_counter += 1
    cid = base if base not in STORE.conversations else f"{base}_{STORE.conv_counter}"
    return cid


@app.post("/v1/tick")
async def tick(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    now = _parse(body.get("now")) or _now_utc()
    trigger_ids = body.get("available_triggers") or []

    with STORE.lock:
        candidates = []
        for tid in trigger_ids:
            trg = STORE.get("trigger", tid)
            if not trg:
                continue
            key = trg.get("suppression_key") or f"trg:{tid}"
            if key in STORE.sent_keys:
                continue
            # expires_at is advisory: the judge's available_triggers list is its own "active right now" view,
            # and its clock may be real time while trigger dates are simulated. Trust the judge's list.
            mid = trg.get("merchant_id") or (trg.get("payload") or {}).get("merchant_id")
            merchant = STORE.get("merchant", mid)
            if not merchant:
                continue
            blocked = STORE.merchant_blocked_until.get(mid)
            if blocked and blocked > now:
                continue
            category = STORE.category_for(merchant, trg)
            if not category:
                continue
            cust_id = trg.get("customer_id")
            customer = STORE.get("customer", cust_id) if cust_id else None
            if cust_id and not customer:
                continue  # wait until the customer context arrives
            candidates.append((trg, merchant, category, customer, key))

        # Highest urgency first; capped per recipient per tick (no spam). Customer-facing sends count per customer.
        candidates.sort(key=lambda x: -(x[0].get("urgency") or 0))
        used_recipients: dict[str, int] = {}
        actions = []
        for trg, merchant, category, customer, key in candidates:
            if len(actions) >= MAX_ACTIONS_PER_TICK:
                break
            mid = merchant.get("merchant_id") or trg.get("merchant_id")
            recipient = f"c:{customer['customer_id']}" if customer else f"m:{mid}"
            if used_recipients.get(recipient, 0) >= (1 if customer else MAX_PER_RECIPIENT_PER_TICK):
                continue
            try:
                msg = composer.compose(category, merchant, trg, customer, now=now)
            except Exception as e:  # never let one bad context kill the tick
                print(f"compose failed for {trg.get('id')}: {e!r}")
                continue
            if composer.validate(msg, category):
                continue
            prior = STORE.merchant_bodies.setdefault(recipient, set())
            if msg["body"] in prior:        # never send the same text twice to the same person
                STORE.sent_keys.add(key)
                continue
            prior.add(msg["body"])
            used_recipients[recipient] = used_recipients.get(recipient, 0) + 1
            conv_id = _conv_id(mid, trg, customer.get("customer_id") if customer else None)
            state = new_state(conv_id, mid, customer.get("customer_id") if customer else None, trg, msg)
            state["trigger_payload"] = trg.get("payload") or {}
            STORE.conversations[conv_id] = state
            STORE.sent_keys.add(key)
            actions.append({
                "conversation_id": conv_id,
                "merchant_id": mid,
                "customer_id": customer.get("customer_id") if customer else None,
                "send_as": msg["send_as"],
                "trigger_id": trg.get("id"),
                "template_name": msg["template_name"],
                "template_params": msg["template_params"],
                "body": msg["body"],
                "cta": msg["cta"],
                "suppression_key": msg["suppression_key"],
                "rationale": msg["rationale"],
            })
    return {"actions": actions}


@app.post("/v1/reply")
async def reply(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"action": "end", "rationale": "invalid_json"})
    conv_id = body.get("conversation_id") or "conv_unknown"
    message = str(body.get("message") or "")
    with STORE.lock:
        state = STORE.conversations.get(conv_id)
        mid = body.get("merchant_id") or (state or {}).get("merchant_id")
        if state is None:
            state = new_state(conv_id, mid, body.get("customer_id"))
            STORE.conversations[conv_id] = state
        merchant = STORE.get("merchant", mid) or {}
        trigger = STORE.get("trigger", state.get("trigger_id")) or {}
        # Always compose replies against the *latest* context versions.
        state["merchant"] = merchant
        state["category"] = STORE.category_for(merchant, trigger)
        state["customer"] = STORE.get("customer", state.get("customer_id") or body.get("customer_id"))
        state["from_role"] = body.get("from_role", "merchant")
        state["merchant_inbound"] = STORE.merchant_inbound.get(f"{mid}:{state['from_role']}", [])
        try:
            result = respond(state, message)
        except Exception as e:
            print(f"respond failed for {conv_id}: {e!r}")
            result = {"action": "wait", "wait_seconds": 3600, "rationale": "Internal error; backing off rather than sending something wrong."}
        STORE.merchant_inbound.setdefault(f"{mid}:{state['from_role']}", []).append(message)
        if state.pop("suppress_merchant", False) and mid and state["from_role"] == "merchant":
            STORE.merchant_blocked_until[mid] = _now_utc() + timedelta(days=OPT_OUT_DAYS)
        for k in ("merchant", "category", "customer", "merchant_inbound"):
            state.pop(k, None)
    if result.get("action") == "send" and not str(result.get("body", "")).strip():
        result = {"action": "end", "rationale": "Nothing useful left to say."}
    return result


@app.post("/v1/teardown")
def teardown():
    with STORE.lock:
        STORE.reset()
    return {"status": "wiped"}


if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
