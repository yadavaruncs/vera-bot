"""
Multi-turn handling: respond(state, merchant_message) -> {"action": "send"|"wait"|"end", ...}

Order of checks (first match wins) — the order is the policy:
  1. conversation already closed            -> end
  2. opt-out / hard no                      -> end   (merchant suppressed upstream)
  3. auto-reply (pattern or verbatim repeat)-> 1st: one owner-flag nudge, 2nd: wait 24h, 3rd+: end
  4. hostile without opt-out                -> one short apology + STOP path; 2nd time -> end
  5. commitment / intent ("yes", "let's do it", "judna hai")
                                            -> ACTION mode: deliver the artifact now, no more qualifying
  6. confirmation after action              -> done + report-back promise; then graceful close
  7. defer ("busy", "baad mein")            -> wait
  8. off-topic ask (GST, loans, …)          -> polite decline + redirect to the thread's one CTA
  9. on-topic question                      -> grounded answer (+ optional LLM) + same CTA
 10. anything else                          -> acknowledge + advance one step; cap total bot turns
"""

from __future__ import annotations

import re
from typing import Any, Optional

from composer import Ctx, clean_body, humanize, inr, num

MAX_BOT_TURNS = 5

AUTO_REPLY_PATTERNS = [
    r"thank(s| you) for (contacting|reaching|your (message|enquiry|inquiry)|messaging|getting in touch)",
    r"(will|shall) (get back|respond|reply|revert|contact you)",
    r"our (team|executive|representative) will",
    r"automated (assistant|message|reply|response)", r"auto[- ]?(reply|response|generated)",
    r"currently (unavailable|closed|away|not available)", r"(outside|after) (our )?(business|working) hours",
    r"we are (closed|away)", r"out of (the )?office",
    r"jaankari ke liye .*shukriya", r"team tak pahuncha", r"madad ke liye shukriya.*(automated|assistant)",
    r"welcome to .{0,40}(how (can|may) (we|i) help)", r"this is an automated",
    r"for (faster|quick) (service|response).{0,30}(call|visit)",
]
OPT_OUT_PATTERNS = [
    r"\bstop\b", r"\bunsubscribe\b", r"\bopt[- ]?out\b", r"\bblock(ed)?\b", r"not interested",
    r"(don'?t|do not|stop|never) (message|messaging|text|texting|send|sending|contact|call)",
    r"leave me alone", r"remove me", r"no more messages", r"mat bhej", r"band karo", r"nahi chahiye",
    r"interested nahi", r"mujhe nahi chahiye", r"pareshan mat",
]
HOSTILE_PATTERNS = [
    r"useless", r"\bspam\b", r"idiot", r"stupid", r"nonsense", r"bakwas", r"pagal", r"\bfraud\b", r"\bscam\b",
    r"shut up", r"irritat", r"bother", r"harass", r"waste of (my )?time", r"bekaar|bekar", r"chutiya|bc\b|mc\b|f+u+c+k",
    r"annoying", r"rubbish", r"get lost",
]
COMMIT_PATTERNS = [
    r"^(yes|yeah|yep|yup|ya|ok|okay|okk+|sure|haan|han|haa|ji|ji haan|done|go|chalo|chalega|theek|thik|confirm|confirmed|proceed|agreed|perfect|great)\b",
    r"let'?s (do|go|start)", r"lets do", r"go ahead", r"do it", r"please (do|send|go|proceed|start|draft|share)",
    r"send (it|me|the|now)", r"draft (it|the)", r"what'?s next", r"whats next", r"how do we start", r"sign me up",
    r"\bjoin\b", r"judna|judrna|jodna|jud(na)? hai", r"karo|kar do|kardo|bhej do|bhejo|shuru kar",
    r"i want (to|it|this)", r"i'?m in\b", r"sounds good", r"book (it|me)", r"^\s*👍", r"^\s*✅",
]
DEFER_PATTERNS = [
    r"\blater\b", r"\bbusy\b", r"baad me(in)?", r"abhi nahi", r"not now", r"in a meeting", r"call (me )?later",
    r"\btomorrow\b", r"\bkal\b", r"next week", r"after (lunch|diwali|some time)", r"give me (some )?time",
]
NO_PATTERNS = [r"^(no|nope|nah|nahi|na|not needed|no thanks|no thank you)\b"]
OFF_TOPIC = {
    "gst": "GST filing", "income tax": "income-tax work", "\\bitr\\b": "ITR filing", "\\btax\\b": "tax work",
    "\\bloan": "loans", "insurance": "insurance", "visa": "visa work", "passport": "passport work",
    "electricity bill": "bill payments", "recharge": "recharges", "stock market|shares|crypto": "investments",
    "lawyer|legal notice|court": "legal matters", "accountant|accounting|payroll|\\bca\\b": "accounting",
    "cricket score|match score": "live scores", "job|hiring|salary": "hiring",
}
THANKS_PATTERNS = [r"^(thanks|thank you|thx|ty|shukriya|dhanyavaad|dhanyawad|great thanks|ok thanks|👍|🙏)\W*$"]
HINDI_MARKERS = (r"\b(hai|hain|kya|nahi|nahin|karo|kar|mujhe|aap|aapka|aapki|haan|chahiye|kaise|kitna|kitne|kab|bhai|ji|"
                 r"mera|meri|hum|kyun|abhi|baad|theek|accha|acha|bhejo|batao|samajh|mein|lagega|hoga|hona|hone|gaya|"
                 r"raha|rahi|toh|yeh|woh|kuch|sab|lekin|aur|wala|wali|dijiye|karein|karna|hoon)\b")

PROGRESSIVE = {
    "draft": "Drafting", "send": "Sending", "put": "Putting", "push": "Pushing", "set": "Setting",
    "publish": "Publishing", "pull": "Pulling", "start": "Starting", "restart": "Restarting", "turn": "Turning",
    "pin": "Pinning", "lock": "Locking", "save": "Saving", "add": "Adding", "walk": "Walking", "get": "Getting",
    "list": "Listing", "book": "Booking", "hold": "Holding", "reserve": "Reserving", "confirm": "Confirming",
}


def _any(patterns, text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9ऀ-ॿ ]+", "", (text or "").lower()).strip()


def is_hindi(text: str) -> bool:
    return bool(re.search(r"[ऀ-ॿ]", text or "")) or len(re.findall(HINDI_MARKERS, text or "", re.I)) >= 2


def to_progressive(ask: str) -> str:
    ask = (ask or "").strip()
    if not ask:
        return "Getting started"
    first, _, rest = ask.partition(" ")
    if first.lower() in PROGRESSIVE:
        out = f"{PROGRESSIVE[first.lower()]} {rest}".strip()
    else:
        out = ask[0].upper() + ask[1:]
    for verb, prog in PROGRESSIVE.items():   # "... and draft X" -> "... and drafting X"
        out = re.sub(rf"(\band|\+) {verb}\b", rf"\1 {prog.lower()}", out)
    return out


IMPERATIVE = {v.lower(): k for k, v in PROGRESSIVE.items()}


def to_imperative(text: str) -> str:
    first, _, rest = (text or "").strip().partition(" ")
    return f"{IMPERATIVE.get(first.lower(), first)} {rest}".strip()


def classify(message: str, prior_inbound: list[str]) -> str:
    m = (message or "").strip()
    low = m.lower()
    if not m:
        return "empty"
    if _any(AUTO_REPLY_PATTERNS, low):
        return "auto_reply"
    if prior_inbound and _norm(m) and sum(1 for p in prior_inbound if _norm(p) == _norm(m)) >= 1 and len(m) > 25:
        return "auto_reply"                      # same long message verbatim again → canned
    if _any(OPT_OUT_PATTERNS, low):
        return "opt_out"
    if _any(HOSTILE_PATTERNS, low):
        return "hostile"
    for pat, label in OFF_TOPIC.items():
        if re.search(pat, low):
            return "off_topic"
    if _any(THANKS_PATTERNS, low):
        return "thanks"
    if _any(DEFER_PATTERNS, low) and not _any(COMMIT_PATTERNS[1:], low):
        return "defer"
    if _any(NO_PATTERNS, low):
        return "no"
    if re.fullmatch(r"\s*[1-9]\s*", m):
        return "slot_pick"
    if _any(COMMIT_PATTERNS, low):
        return "commit"
    if "?" in m or re.match(r"^(what|how|why|when|where|which|who|can|is|are|does|do|kya|kaise|kitna|kitne|kab|kaun|kyun)\b", low):
        return "question"
    return "other"


def off_topic_label(message: str) -> str:
    low = message.lower()
    for pat, label in OFF_TOPIC.items():
        if re.search(pat, low):
            return label
    return "that"


# ---------------------------------------------------------------------------


def new_state(conversation_id: str, merchant_id: Optional[str], customer_id: Optional[str] = None,
              trigger: Optional[dict] = None, composed: Optional[dict] = None) -> dict:
    return {
        "conversation_id": conversation_id,
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "trigger_id": (trigger or {}).get("id"),
        "kind": (trigger or {}).get("kind", "generic"),
        "send_as": (composed or {}).get("send_as", "vera"),
        "followup": (composed or {}).get("followup", {}),
        "cta": (composed or {}).get("cta"),
        "last_ask": ((composed or {}).get("template_params") or [None, None, None])[-1],
        "sent_bodies": [composed["body"]] if composed else [],
        "turns": [{"from": "bot", "body": composed["body"]}] if composed else [],
        "stage": "pitch",          # pitch -> action -> confirmed -> closed
        "status": "active",        # active | waiting | ended
        "auto_reply_count": 0,
        "hostile_count": 0,
        "bot_turns": 1 if composed else 0,
        "lang": "hinglish" if (composed or {}).get("lang") in ("hinglish", "hindi") else (composed or {}).get("lang"),
        # injected by caller each turn (not persisted semantics): category, merchant, customer, merchant_inbound
    }


def _ctx(state: dict) -> Ctx:
    return Ctx(state.get("category") or {}, state.get("merchant") or {},
               {"kind": state.get("kind"), "payload": {}}, state.get("customer"))


def _send(state: dict, body: str, cta: str, rationale: str) -> dict:
    body = clean_body(body)
    if body in state["sent_bodies"]:
        # Anti-repetition: never send the same text twice in one conversation.
        body = clean_body(body + " (Just making sure this reached you.)")
        if body in state["sent_bodies"]:
            return {"action": "end", "rationale": "Would have repeated an earlier message verbatim; closing instead."}
    state["sent_bodies"].append(body)
    state["turns"].append({"from": "bot", "body": body})
    state["bot_turns"] = state.get("bot_turns", 0) + 1
    return {"action": "send", "body": body, "cta": cta, "rationale": rationale}


def _end(state: dict, rationale: str, suppress: bool = False) -> dict:
    state["status"] = "ended"
    state["stage"] = "closed"
    if suppress:
        state["suppress_merchant"] = True
    return {"action": "end", "rationale": rationale}


def _post_artifact(ctx: Ctx) -> str:
    offer = ctx.active_offers[0] if ctx.active_offers else ctx.catalog_offer()
    if offer:
        return f"Google post draft: \"{offer} at {ctx.name}, {ctx.locality}. Walk in or call to book.\""
    return f"Google post draft: \"{ctx.name}, {ctx.locality} — open today. Call or walk in.\""


def _action_reply(state: dict, ctx: Ctx, message: str, hindi: bool) -> dict:
    follow = state.get("followup") or {}
    low = message.lower()
    customer_side = state.get("send_as") == "merchant_on_behalf" or state.get("from_role") == "customer"
    if customer_side:
        body = ("Confirmed ✅ Hum aapka slot hold kar rahe hain — ek din pehle reminder bhej denge."
                if hindi else "Confirmed ✅ We're holding it for you and will send a reminder the day before. See you soon!")
        state["stage"] = "confirmed"
        return _send(state, body, "none", "Customer said yes; confirming the booking, no further asks.")

    if re.search(r"\bjoin\b|judna|judrna|jodna|sign me up", low):
        body = (f"Badhiya! Setup abhi shuru kar rahi hoon — step 1: {ctx.name}"
                + (f", {ctx.locality}" if ctx.locality else "") + " ka Google listing pull karke profile pre-fill kar rahi hoon. "
                "Step 2: owner number pe 2-minute ka sign-up form. Reply CONFIRM aur main form bhej deti hoon."
                if hindi else
                f"Great — starting your setup now. Step 1: I'm pulling the Google listing for {ctx.name}"
                + (f", {ctx.locality}" if ctx.locality else "") + " to pre-fill your profile. "
                "Step 2: a 2-minute sign-up form on the owner's number. Reply CONFIRM and I'll send the form.")
        state["stage"] = "action"
        return _send(state, body, "binary_confirm_cancel", "Explicit join intent → switched straight to onboarding action, no qualifying questions.")

    deliverable = follow.get("deliverable") or "setting up the next step"
    artifact = follow.get("artifact") or _post_artifact(ctx)
    lead = to_progressive(deliverable)
    if hindi:
        body = f"Done, shuru kar diya. {lead} — yeh raha draft:\n{artifact}\nReply CONFIRM aur main ise live kar dungi, ya jo badlaav chahiye woh bata dijiye."
    else:
        body = f"On it. {lead} — here's the draft:\n{artifact}\nReply CONFIRM and it goes live, or send me any edits."
    state["stage"] = "action"
    return _send(state, body, "binary_confirm_cancel",
                 "Merchant committed → action mode: delivered the concrete artifact immediately instead of asking more questions.")


def _confirmed_reply(state: dict, ctx: Ctx, hindi: bool) -> dict:
    state["stage"] = "confirmed"
    views = ctx.perf.get("views")
    body = (f"Ho gaya ✅ Live kar diya. 7 din baad main results share karungi"
            + (f" — abhi ke {num(views)} monthly views ke against compare karke." if views else ".")
            if hindi else
            "Done ✅ It's live. I'll report back in 7 days"
            + (f" with the numbers against your current {num(views)} monthly views." if views else "."))
    return _send(state, body, "none", "Merchant confirmed; executed and set a concrete report-back so the loop closes.")


def _answer_question(state: dict, ctx: Ctx, message: str, hindi: bool) -> dict:
    low = message.lower()
    follow = state.get("followup") or {}
    ask = follow.get("deliverable") or "set this up for you"
    if state.get("stage") == "action":
        cta_line = ("Reply CONFIRM aur main live kar dungi." if hindi else "Reply CONFIRM and it goes live.")
    else:
        cta_line = ("Aage badhein? Reply YES." if hindi else "Shall I go ahead? Reply YES.")
    ask = to_imperative(ask)
    answer = None
    if re.search(r"how long|how much time|kitna time|kitne din|kab tak|kitni der|when will .*(live|show|visible)", low):
        answer = ("Aapke CONFIRM ke turant baad main publish kar deti hoon; Google pe dikhne mein usually 24-48 ghante lagte hain."
                  if hindi else "I publish the moment you confirm; Google usually takes 24-48 hours to show changes.")
    elif re.search(r"price|cost|charge|fee|kitna|kitne|paisa|paise|how much", low):
        renew = None
        for key in ("renewal_amount",):
            renew = (state.get("trigger_payload") or {}).get(key)
        if renew:
            answer = f"Renewal for your {ctx.sub.get('plan', '')} plan is {inr(renew)}."
        elif ctx.active_offers:
            answer = (f"Abhi aapki listing pe \"{ctx.active_offers[0]}\" live hai; drafting aur posting main kar deti hoon." if hindi
                      else f"The offer live on your listing right now is \"{ctx.active_offers[0]}\"; drafting and posting is part of what I do for you.")
        else:
            answer = ("Price pe main guess nahi karungi — team se exact figure lekar yahin share karti hoon." if hindi
                      else "I won't guess at a price — I'll get the exact figure from the team and share it here.")
    elif re.search(r"who are you|kaun|what is vera|are you a bot|are you human", low):
        answer = "I'm Vera, magicpin's assistant — I look after your Google profile, posts and customer messages."
    elif re.search(r"how|kaise|what will you|kya karoge|what happens", low):
        answer = ("Simple hai: main sab ready karti hoon, aap ek reply se approve karte hain, tabhi live hota hai — draft pehle aapko dikhaungi."
                  if hindi else
                  f"Simple: I prepare it ({ask}), you approve with one reply, and only then does it go live — you see the full draft first.")
    elif re.search(r"(where|kahan).*(data|number|figure)|source|how do you know", low):
        answer = "These numbers come from your own Google listing performance and magicpin's category benchmarks for your area."
    if answer is None:
        try:
            from llm import answer_question  # optional, only if an API key is configured
            answer = answer_question(state, ctx, message)
        except Exception:
            answer = None
    if not answer:
        answer = ("Achha sawaal — main team se confirm karke exact jawab deti hoon." if hindi
                  else "Good question — I'll confirm the exact answer with the team rather than guess.")
    return _send(state, f"{answer} {cta_line}", "binary_yes_stop", "Answered the question from context only (no guessing), then restated the single CTA.")


def respond(state: dict, merchant_message: str) -> dict:
    """Given the conversation so far + the merchant's latest message, produce the reply."""
    message = merchant_message or ""
    prior = list(state.get("merchant_inbound") or [])
    state.setdefault("turns", []).append({"from": state.get("from_role", "merchant"), "body": message})
    ctx = _ctx(state)
    # Per-turn language: follow the merchant's latest message; very short replies ("ok", "yes") keep the prior language.
    if is_hindi(message):
        state["lang"] = "hinglish"
    elif len(message.split()) > 3:
        state["lang"] = "en"
    hindi = state.get("lang") == "hinglish"

    if state.get("status") == "ended":
        return {"action": "end", "rationale": "Conversation already closed; not re-engaging."}

    label = classify(message, prior)
    state["last_label"] = label

    if label == "opt_out":
        return _end(state, "Merchant opted out / said not interested → closing and suppressing further sends to this merchant.", suppress=True)

    if label == "auto_reply":
        # Count across conversations too: the same canned text from this merchant's number is one signal.
        prior_auto = sum(1 for p in prior if _any(AUTO_REPLY_PATTERNS, p.lower()) or _norm(p) == _norm(message))
        state["auto_reply_count"] = max(state.get("auto_reply_count", 0), prior_auto) + 1
        n = state["auto_reply_count"]
        if n == 1:
            body = ("Lagta hai yeh auto-reply hai 🙂 Owner/manager dekhein toh bas YES reply kar dein — baaki main sambhal lungi."
                    if (hindi or ctx.lang == "hinglish") else
                    "Looks like an auto-reply 🙂 When the owner or manager sees this, just reply YES and I'll take it from there.")
            return _send(state, body, "binary_yes_stop", "Detected WhatsApp Business auto-reply; one short owner-flag nudge, no re-pitch.")
        if n == 2:
            state["status"] = "waiting"
            return {"action": "wait", "wait_seconds": 86400,
                    "rationale": "Auto-reply again → owner not at the phone. Backing off 24h instead of burning turns."}
        return _end(state, f"Auto-reply {n}x with no human reply → closing the conversation gracefully.")

    if label == "hostile":
        state["hostile_count"] = state.get("hostile_count", 0) + 1
        if state["hostile_count"] >= 2 or state.get("bot_turns", 0) >= 3:
            return _end(state, "Repeated frustration → exiting without further messages.", suppress=True)
        body = ("Maaf kijiye, aapko pareshan nahi karna tha. Agar aap nahi chahte toh STOP reply karein — main dobara message nahi karungi. 🙏"
                if hindi else
                "Sorry — didn't mean to bother you. If you'd rather not hear from me, reply STOP and I won't message again. 🙏")
        return _send(state, body, "none", "Frustration detected: short apology + explicit opt-out path; no pitch.")

    if label == "no":
        return _end(state, "Merchant declined; closing politely without pushing.")

    if label == "thanks" and state.get("stage") in ("action", "confirmed"):
        who = "Customer" if state.get("from_role") == "customer" else "Merchant"
        return _end(state, f"{who} acknowledged after the work was delivered; natural close.")

    if label == "defer":
        low = message.lower()
        secs = 86400 if re.search(r"tomorrow|\bkal\b", low) else 604800 if "next week" in low else 14400
        state["status"] = "waiting"
        return {"action": "wait", "wait_seconds": secs, "rationale": f"Merchant asked for time; backing off {secs // 3600}h."}

    if state.get("bot_turns", 0) >= MAX_BOT_TURNS:
        return _end(state, f"Reached {MAX_BOT_TURNS} bot turns in this conversation; closing to avoid over-messaging.")

    if label == "off_topic":
        what = off_topic_label(message)
        follow = state.get("followup") or {}
        back = follow.get("deliverable")
        body = ((f"{what} mein main madad nahi kar paungi — uske liye aapke CA/expert best rahenge. "
                 + (f"Hamari baat pe wapas — want me to {to_imperative(back)}? Reply YES." if back else "Profile ya customers se juda kuch ho toh bataiye."))
                if hindi else
                (f"{what[0].upper() + what[1:]} is outside what I can help with — your CA or a specialist is the right person for that. "
                 + (f"Back to our thread — want me to {to_imperative(back)}? Reply YES." if back
                    else "Anything on your Google profile, offers or customer messages, I'm on it.")))
        return _send(state, body, "binary_yes_stop" if back else "open_ended",
                     "Out-of-scope ask declined politely; redirected to the original thread's single CTA.")

    if label in ("commit", "slot_pick"):
        if label == "slot_pick" and state.get("send_as") == "merchant_on_behalf":
            slots = [s.get("label") for s in ((state.get("trigger_payload") or {}).get("available_slots") or [])]
            idx = int(message.strip()) - 1
            if 0 <= idx < len(slots):
                state["stage"] = "confirmed"
                body = (f"Booked ✅ {slots[idx]}. Ek din pehle reminder bhej denge." if (hindi or state.get("lang") == "hinglish")
                        else f"Booked ✅ {slots[idx]}. We'll send a reminder the day before.")
                return _send(state, body, "none", "Customer picked a slot; confirmed it exactly.")
        if state.get("stage") == "action" and re.search(r"confirm|yes|haan|ok|go|done|live|publish|send", message.lower()):
            return _confirmed_reply(state, ctx, hindi)
        if state.get("stage") == "confirmed":
            return _end(state, "Work already confirmed; nothing further to add.")
        return _action_reply(state, ctx, message, hindi)

    if label == "question":
        return _answer_question(state, ctx, message, hindi)

    # "other": acknowledge and move exactly one step forward
    if state.get("stage") == "confirmed":
        return _end(state, "Loop already closed with a delivered action.")
    follow = state.get("followup") or {}
    back = follow.get("deliverable")
    body = ("Samajh gayi. Jab ready hon, bas YES reply kar dijiye — baaki main sambhal lungi."
            if hindi else
            (f"Got it. Whenever you're ready, reply YES and I'll {to_imperative(back)}." if back
             else "Got it. Whenever you're ready, reply YES and I'll take it from there."))
    return _send(state, body, "binary_yes_stop", "Neutral reply; acknowledged and kept a single low-friction CTA open.")
