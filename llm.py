"""
Optional LLM fallback for free-form merchant questions the rule layer can't answer.

Off by default. Enabled only when ANTHROPIC_API_KEY is set. temperature=0, hard 8s timeout,
and the answer is rejected (→ caller falls back to a safe "I'll confirm" line) if it contains a URL,
is too long, or mentions any number that isn't present in the contexts it was given.
"""

from __future__ import annotations

import json
import os
import re
from urllib import request as urlrequest

MODEL = os.environ.get("VERA_LLM_MODEL", "claude-sonnet-5")
TIMEOUT = float(os.environ.get("VERA_LLM_TIMEOUT", "8"))

SYSTEM = (
    "You are Vera, magicpin's WhatsApp assistant for Indian merchants. Answer the merchant's question in 1-2 short "
    "sentences using ONLY facts in the provided context. If the context doesn't contain the answer, say you'll "
    "confirm with the team. Peer tone, no hype, no URLs, no invented numbers, no re-introduction. Match the merchant's "
    "language (Hindi-English mix if they wrote in Hinglish). Do not add a call-to-action."
)


def answer_question(state: dict, ctx, message: str) -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    facts = {
        "merchant": {k: ctx.m.get(k) for k in ("identity", "subscription", "performance", "offers",
                                               "customer_aggregate", "signals", "review_themes")},
        "category_peer_stats": ctx.peer,
        "thread_topic": state.get("kind"),
        "what_vera_offered": (state.get("followup") or {}).get("deliverable"),
        "conversation": state.get("turns", [])[-6:],
    }
    facts_json = json.dumps(facts, ensure_ascii=False)
    body = {
        "model": MODEL, "max_tokens": 200, "temperature": 0, "system": SYSTEM,
        "messages": [{"role": "user", "content": f"CONTEXT:\n{facts_json}\n\nMERCHANT ASKS: {message}"}],
    }
    req = urlrequest.Request(
        "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode("utf-8"),
        headers={"x-api-key": key, "content-type": "application/json", "anthropic-version": "2023-06-01"})
    with urlrequest.urlopen(req, timeout=TIMEOUT) as resp:
        text = json.loads(resp.read().decode("utf-8"))["content"][0]["text"].strip()
    if not text or len(text) > 400 or re.search(r"https?://|www\.", text):
        return None
    for n in re.findall(r"\d[\d,.]*", text):
        if n.replace(",", "").rstrip(".") not in facts_json.replace(",", ""):
            return None  # number not grounded in context → reject
    return text
