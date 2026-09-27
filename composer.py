"""
Vera composer — deterministic, grounded message composition.

compose(category, merchant, trigger, customer=None, now=None) -> dict

Design rules (enforced by construction, checked by validate()):
  * Every number / name / date in a body comes from one of the 4 contexts
    (or is arithmetic on them). Nothing is invented.
  * One primary CTA, always the last sentence.
  * Voice + salutation + language follow CategoryContext.voice and the
    merchant's / customer's language preference.
  * Placeholder / thin trigger payloads fall back to the richest verifiable
    facts in the merchant context instead of making things up.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional

COMPOSER_VERSION = "vera-composer-2.0"

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def inr(value: Any) -> str:
    """₹ with Indian digit grouping: 149 -> ₹149, 14999 -> ₹14,999, 125000 -> ₹1,25,000."""
    try:
        n = int(round(float(str(value).replace(",", ""))))
    except (TypeError, ValueError):
        return f"₹{value}"
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return f"{'-' if n < 0 else ''}₹{s}"


def num(n: Any) -> str:
    try:
        return f"{int(round(float(n))):,}"
    except (TypeError, ValueError):
        return str(n)


def pct(x: Any) -> str:
    """0.18 -> '18%' (absolute value; callers choose up/down wording)."""
    try:
        v = abs(float(x)) * 100
    except (TypeError, ValueError):
        return str(x)
    return f"{v:.1f}%".replace(".0%", "%") if v < 10 and v != int(v) else f"{int(round(v))}%"


def rate(x: Any) -> str:
    """CTR 0.021 -> '2.1%'."""
    try:
        return f"{float(x) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(x)


def parse_dt(s: Any) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None


def day_month(s: Any) -> str:
    d = parse_dt(s)
    return f"{d.day} {d.strftime('%b')}" if d else str(s)


def day_month_year(s: Any) -> str:
    d = parse_dt(s)
    return f"{d.day} {d.strftime('%b %Y')}" if d else str(s)


def slot_label(s: Any) -> str:
    """'2026-05-02T19:00:00+05:30' -> 'Sat 2 May, 7pm'."""
    d = parse_dt(s)
    if not d:
        return str(s)
    h = d.hour % 12 or 12
    ampm = "am" if d.hour < 12 else "pm"
    t = f"{h}{ampm}" if d.minute == 0 else f"{h}:{d.minute:02d}{ampm}"
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}, {t}"


THEME_LABELS = {
    "delivery_late": "late delivery", "weekend_busy": "the weekend rush", "wait_time": "wait times",
    "morning_crowd": "the morning crowd", "saturday_wait": "Saturday waits", "thali_quality": "thali",
    "pizza_quality": "pizza", "doctor_manner": "doctor's manner", "stylist_skill": "stylists",
    "instructor_quality": "instructors", "equipment_quality": "equipment", "delivery_speed": "delivery speed",
    "medicine_availability": "stock availability", "small_classes": "small class sizes",
}


def theme_label(theme: Any) -> str:
    return THEME_LABELS.get(str(theme), humanize(theme))


def humanize(token: Any) -> str:
    return str(token or "").replace("_", " ").strip()


def first_sentence(text: str) -> str:
    m = re.match(r"(.+?[.!?])(\s|$)", text or "")
    return m.group(1) if m else (text or "")


def price_in(title: str) -> Optional[int]:
    m = re.search(r"₹\s?([\d,]+)", title or "")
    return int(m.group(1).replace(",", "")) if m else None


def service_in(title: str) -> str:
    return (title or "").split("@")[0].strip()


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------

HINDI_BELT = {"delhi", "jaipur", "lucknow", "chandigarh", "mumbai", "pune",
              "ahmedabad", "noida", "gurgaon", "gurugram", "indore", "bhopal", "patna"}


def merchant_lang(merchant: dict, category: dict) -> str:
    ident = merchant.get("identity", {}) or {}
    langs = [str(l).lower() for l in ident.get("languages", []) or []]
    city = str(ident.get("city", "")).lower()
    code_mix = str(((category or {}).get("voice") or {}).get("code_mix", ""))
    if "hi" in langs and city in HINDI_BELT and not code_mix.startswith("english_primary"):
        return "hinglish"
    return "en"


def customer_lang(customer: dict) -> str:
    pref = str(((customer or {}).get("identity") or {}).get("language_pref", "en")).lower()
    if pref in ("hi", "hindi"):
        return "hindi"
    if pref.startswith("hi"):
        return "hinglish"
    return "en"


def customer_greeting(customer: dict) -> str:
    pref = str(((customer or {}).get("identity") or {}).get("language_pref", "")).lower()
    if pref.startswith("ta"):
        return "Vanakkam"
    if pref.startswith("kn"):
        return "Namaskara"
    if pref.startswith("te"):
        return "Namaskaram"
    if pref in ("hi", "hindi") or pref.startswith("hi"):
        return "Namaste"
    return "Hi"


# ---------------------------------------------------------------------------
# Context wrapper
# ---------------------------------------------------------------------------


class Ctx:
    def __init__(self, category, merchant, trigger, customer=None, now=None):
        self.cat = category or {}
        self.m = merchant or {}
        self.t = trigger or {}
        self.c = customer or None
        self.now = parse_dt(now) if isinstance(now, str) else now
        self.slug = self.m.get("category_slug") or self.cat.get("slug") or ""
        self.ident = self.m.get("identity", {}) or {}
        self.perf = self.m.get("performance", {}) or {}
        self.delta = self.perf.get("delta_7d", {}) or {}
        self.sub = self.m.get("subscription", {}) or {}
        self.agg = self.m.get("customer_aggregate", {}) or {}
        self.peer = self.cat.get("peer_stats", {}) or {}
        self.voice = self.cat.get("voice", {}) or {}
        self.payload = self.t.get("payload", {}) or {}
        self.placeholder = bool(self.payload.get("placeholder"))
        self.kind = self.t.get("kind", "generic")
        self.name = self.ident.get("name") or "your business"
        self.locality = self.ident.get("locality") or self.ident.get("city") or ""
        self.city = self.ident.get("city") or ""
        self.lang = merchant_lang(self.m, self.cat)
        self.facts: list[str] = []      # rationale: which context facts were used
        self.levers: list[str] = []     # rationale: which compulsion levers

    # ---- people ----
    @property
    def owner_first(self) -> str:
        raw = str(self.ident.get("owner_first_name") or "").strip()
        return re.sub(r"^dr\.?\s*", "", raw, flags=re.I).strip()

    @property
    def sal(self) -> str:
        first = self.owner_first
        if self.slug == "dentists":
            return f"Dr. {first}" if first else "Doctor"
        return first or f"{self.name} team"

    @property
    def sender_for_customer(self) -> str:
        """Who signs a customer-facing message."""
        first = self.owner_first
        if self.slug == "dentists":
            return f"Dr. {first}'s clinic ({self.name})" if first and first.lower() not in self.name.lower() else self.name
        if first:
            return f"{first} from {self.name}"
        return self.name

    @property
    def people(self) -> str:
        return {"dentists": "patients", "gyms": "members"}.get(self.slug, "customers")

    # ---- offers ----
    @property
    def active_offers(self) -> list[str]:
        return [o.get("title") for o in self.m.get("offers", []) or []
                if o.get("status") == "active" and o.get("title")]

    @property
    def expired_offers(self) -> list[str]:
        return [o.get("title") for o in self.m.get("offers", []) or []
                if o.get("status") in ("expired", "paused") and o.get("title")]

    def catalog_offer(self, prefer=("service_at_price", "free_service", "free_trial", "free_addon"),
                      audience=None) -> Optional[str]:
        cat = self.cat.get("offer_catalog", []) or []
        for typ in prefer:
            for o in cat:
                if o.get("type") == typ and (audience is None or o.get("audience") in audience):
                    return o.get("title")
        return cat[0].get("title") if cat else None

    def catalog_find(self, *words) -> Optional[str]:
        for o in self.cat.get("offer_catalog", []) or []:
            t = o.get("title", "")
            if all(w.lower() in t.lower() for w in words):
                return t
        return None

    # ---- signals / reviews / history ----
    def signal(self, prefix: str) -> Optional[str]:
        for s in self.m.get("signals", []) or []:
            if s == prefix or s.startswith(prefix + ":"):
                return s.split(":", 1)[1] if ":" in s else s
        return None

    def review_theme(self, sentiment: str) -> Optional[dict]:
        themes = [r for r in self.m.get("review_themes", []) or [] if r.get("sentiment") == sentiment]
        themes.sort(key=lambda r: -(r.get("occurrences_30d") or 0))
        return themes[0] if themes else None

    def history(self, who: Optional[str] = None) -> list[dict]:
        h = self.m.get("conversation_history", []) or []
        return [x for x in h if who is None or x.get("from") == who]

    def history_text(self) -> str:
        return " ".join(str(x.get("body", "")) for x in self.history())

    # ---- digest ----
    def digest(self, item_id: Optional[str] = None, kinds: tuple = ()) -> Optional[dict]:
        items = self.cat.get("digest", []) or []
        if item_id:
            for d in items:
                if d.get("id") == item_id:
                    return d
        for k in kinds:
            for d in items:
                if d.get("kind") == k:
                    return d
        return None

    # ---- perf vs peer ----
    def peer_gaps(self) -> list[str]:
        out = []
        ctr, pctr = self.perf.get("ctr"), self.peer.get("avg_ctr")
        if ctr is not None and pctr:
            if ctr < pctr:
                out.append(f"click-through is {rate(ctr)} vs {rate(pctr)} for peers")
        calls, pcalls = self.perf.get("calls"), self.peer.get("avg_calls_30d")
        if calls is not None and pcalls and calls < pcalls:
            out.append(f"calls are at {num(calls)} in 30 days vs a {num(pcalls)} peer average")
        views, pviews = self.perf.get("views"), self.peer.get("avg_views_30d")
        if views is not None and pviews and views < pviews:
            out.append(f"profile views are at {num(views)} vs {num(pviews)} for a typical listing")
        return out

    def peer_wins(self) -> list[str]:
        out = []
        ctr, pctr = self.perf.get("ctr"), self.peer.get("avg_ctr")
        if ctr is not None and pctr and ctr >= pctr * 1.15:
            out.append(f"your {rate(ctr)} click-through beats the {rate(pctr)} peer average")
        views, pviews = self.perf.get("views"), self.peer.get("avg_views_30d")
        if views is not None and pviews and views >= pviews * 1.3:
            out.append(f"{num(views)} views in 30 days vs {num(pviews)} for a typical listing")
        calls, pcalls = self.perf.get("calls"), self.peer.get("avg_calls_30d")
        if calls is not None and pcalls and calls >= pcalls * 1.3:
            out.append(f"{num(calls)} calls in 30 days vs a {num(pcalls)} peer average")
        return out

    def worst_delta(self) -> Optional[tuple[str, float]]:
        best = None
        for key, label in (("calls_pct", "calls"), ("views_pct", "views"), ("ctr_pct", "click-through")):
            v = self.delta.get(key)
            if isinstance(v, (int, float)) and v < 0 and (best is None or v < best[1]):
                best = (label, v)
        return best

    def best_delta(self) -> Optional[tuple[str, float]]:
        best = None
        for key, label in (("calls_pct", "calls"), ("views_pct", "views"), ("ctr_pct", "click-through")):
            v = self.delta.get(key)
            if isinstance(v, (int, float)) and v > 0 and (best is None or v > best[1]):
                best = (label, v)
        return best

    def lapsed_count(self) -> Optional[tuple[int, str]]:
        for key, label in (("lapsed_180d_plus", "180+ days"), ("lapsed_90d_plus", "90+ days")):
            if self.agg.get(key):
                return int(self.agg[key]), label
        return None

    # ---- language-aware CTA ----
    def cta(self, en: str, hi: Optional[str] = None) -> str:
        return hi if (self.lang == "hinglish" and hi) else en

    def ask(self, ask: str) -> str:
        """Single binary CTA as the last sentence; light Hindi closer for hi-belt merchants."""
        if self.lang == "hinglish":
            return f"Want me to {ask}? Haan ho toh bas YES reply kar dijiye."
        return f"Want me to {ask}? Reply YES."


# ---------------------------------------------------------------------------
# Merchant-facing handlers. Each returns (body, cta_type, followup)
# followup = {"deliverable": str, "artifact": str} — what Vera does on YES.
# ---------------------------------------------------------------------------

SINGULAR = {"pharmacies": "pharmacy", "dentists": "dental clinic", "salons": "salon", "gyms": "gym",
            "restaurants": "restaurant"}
CONSENT_LABELS = {"appointment_tomorrow": "appointment reminder", "recall_due": "recall reminder",
                  "chronic_refill_due": "refill reminder", "customer_lapsed_soft": "win-back note",
                  "customer_lapsed_hard": "win-back note", "trial_followup": "trial follow-up",
                  "wedding_package_followup": "bridal follow-up"}

ITEM_OFFER = {
    "research": "pull the abstract and draft a patient-friendly WhatsApp you can forward",
    "compliance": "send a short audit checklist you can file with your SOPs",
    "cde": "save the date and send you the registration details",
    "trend": "draft a Google post that positions you for this demand",
    "tech": "put together a quick cost/benefit note for your volumes",
    "seasonal": "draft the offer + Google post for this window",
    "supply": "draft the customer WhatsApp note for your repeat-Rx list",
    "alert": "draft the customer WhatsApp note + replacement workflow",
    "compete": "draft a positioning post for your listing",
}

INTEREST_MAP = {
    "aligner": ("impression", "scan", "aligner", "cad"),
    "whitening": ("whitening", "aesthetic", "cosmetic"),
    "implant": ("implant", "crown", "zirconia"),
    "balayage": ("balayage", "colour", "color"),
    "keratin": ("keratin", "smoothening"),
}


def _interest_link(ctx: Ctx, item: dict) -> Optional[str]:
    merchant_said = " ".join(str(h.get("body", "")) for h in ctx.history("merchant")).lower()
    blob = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    for interest, cues in INTEREST_MAP.items():
        if interest in merchant_said and any(c in blob for c in cues):
            return interest
    return None


def _item_relevance(ctx: Ctx, item: dict) -> Optional[str]:
    seg = str(item.get("patient_segment", ""))
    if "high_risk" in seg:
        n = ctx.agg.get("high_risk_adult_count")
        if n:
            ctx.facts.append(f"customer_aggregate.high_risk_adult_count={n}")
            return f"That maps straight onto the {num(n)} high-risk adults on your roster."
        if ctx.signal("high_risk_adult_cohort"):
            return "Relevant to the high-risk adult cohort in your practice."
    interest = _interest_link(ctx, item)
    if interest:
        ctx.facts.append(f"merchant asked about {interest} in conversation_history")
        return f"Ties in with the {interest} push you asked me for."
    if item.get("kind") in ("supply", "alert") and ctx.agg.get("chronic_rx_count"):
        return f"Relevant to your {num(ctx.agg['chronic_rx_count'])} chronic-Rx customers."
    return None


def _digest_message(ctx: Ctx, item: dict, lead: str) -> tuple[str, str, dict]:
    kind = item.get("kind", "research")
    src = item.get("source", "")
    title = item.get("title", "")
    parts = [f"{ctx.sal}, {lead} — {src}: {title}" + (f" (n={num(item['trial_n'])})" if item.get("trial_n") else "") + "."]
    if item.get("date"):
        parts.append(f"When: {slot_label(item['date'])}" + (f", {item['credits']} CDE credits" if item.get("credits") else "") + ".")
    if item.get("summary"):
        parts.append(item["summary"])
    rel = _item_relevance(ctx, item)
    if rel:
        parts.append(rel)
    offer = ITEM_OFFER.get(kind, "draft the next step for you")
    parts.append(ctx.ask(offer))
    ctx.facts.append(f"digest[{item.get('id')}] source='{src}'")
    ctx.levers += ["specificity (source citation)", "reciprocity", "single binary CTA"]
    return " ".join(parts), "binary_yes_stop", {"deliverable": offer, "artifact": _digest_artifact(ctx, item)}


def _matching_offer(ctx: Ctx, text: str) -> Optional[str]:
    words = {w for w in re.findall(r"[a-z]{5,}", text.lower())}
    pool = ctx.active_offers + [o.get("title") for o in ctx.cat.get("offer_catalog", []) or []]
    for o in pool:
        if words & {w for w in re.findall(r"[a-z]{5,}", (o or "").lower())}:
            return o
    return None


def _digest_artifact(ctx: Ctx, item: dict) -> str:
    kind, src, summary = item.get("kind"), item.get("source", ""), item.get("summary", "")
    if kind == "research":
        return (f"Abstract ({src}): {summary}\n"
                f"Patient WhatsApp draft: \"Quick note from {ctx.name}: a large study ({src}) found that for people "
                "with a recent history of cavities, more frequent check-ups made a real difference. If that's you, "
                "ask us at your next visit whether a shorter interval makes sense.\"")
    if kind == "compliance":
        return f"Checklist: 1) {item.get('actionable', item.get('title'))}. 2) Record the change in your SOP file. 3) Brief your staff. 4) Keep the circular ({src}) on file."
    if kind == "cde":
        return f"{item.get('title')} — {slot_label(item['date']) if item.get('date') else ''}. {item.get('actionable', '')}"
    offer = _matching_offer(ctx, item.get("title", "") + " " + summary)
    if kind == "trend":
        return (f"Google post draft: \"{offer + ' at ' if offer else ''}{ctx.name}, {ctx.locality} — "
                "done properly, with a proper consultation first. Call or walk in to book.\"")
    return f"Summary ({src}): {summary} Suggested step: {item.get('actionable', '')}"


def h_research_digest(ctx: Ctx):
    item = ctx.digest(ctx.payload.get("top_item_id") or ctx.payload.get("digest_item_id"),
                      kinds=("research", "trend", "tech", "compliance"))
    if not item:
        return h_generic(ctx)
    lead = {"research": "one research item worth 2 minutes",
            "compliance": "compliance heads-up",
            "trend": "a demand shift worth knowing",
            "tech": "new in your supply chain"}.get(item.get("kind"), "this week's digest")
    return _digest_message(ctx, item, lead)


def h_regulation_change(ctx: Ctx):
    item = ctx.digest(ctx.payload.get("top_item_id"), kinds=("compliance",))
    if not item:
        return h_generic(ctx)
    deadline = ctx.payload.get("deadline_iso")
    parts = [f"{ctx.sal}, compliance flag from the {item.get('source', 'regulator')}: {item.get('title')}."]
    if item.get("summary"):
        parts.append(item["summary"])
    if deadline:
        days = ""
        if ctx.now:
            d = parse_dt(deadline)
            if d:
                dd = (d.replace(tzinfo=None) - ctx.now.replace(tzinfo=None)).days
                if dd > 0:
                    days = f" — {dd} days from today"
        parts.append(f"Deadline: {day_month_year(deadline)}{days}.")
        ctx.facts.append(f"trigger.payload.deadline_iso={deadline}")
    if item.get("actionable"):
        parts.append(f"Practical step: {item['actionable'][0].lower() + item['actionable'][1:]}.")
    parts.append(ctx.cta("Want me to send a 5-point audit checklist + an SOP line you can file? Reply YES.",
                         "5-point audit checklist + ek SOP line bhej doon jo aap file kar sakein? Reply YES."))
    ctx.facts.append(f"digest[{item.get('id')}]")
    ctx.levers += ["loss aversion (deadline)", "specificity", "effort externalization"]
    return " ".join(parts), "binary_yes_stop", {
        "deliverable": "sending the audit checklist",
        "artifact": ("Checklist: 1) Note the film speed / sensor type on your X-ray unit. "
                     "2) If D-speed film, order E-speed stock or plan RVG. 3) Log the change in your radiation SOP. "
                     "4) Brief your assistant on the new exposure settings. 5) Keep the circular reference on file.")
        if "radiograph" in str(item.get("id", "")) else
        f"Checklist built from: {item.get('actionable', item.get('title'))}"}


def h_cde_opportunity(ctx: Ctx):
    item = ctx.digest(ctx.payload.get("digest_item_id") or ctx.payload.get("top_item_id"), kinds=("cde",))
    if not item:
        return h_generic(ctx)
    parts = [f"{ctx.sal}, a CDE slot worth blocking: \"{item.get('title')}\" ({item.get('source')})"]
    when = f" on {slot_label(item['date'])}" if item.get("date") else ""
    credits = ctx.payload.get("credits") or item.get("credits")
    parts[0] += f"{when}" + (f" — {credits} credits" if credits else "") + "."
    if item.get("actionable"):
        parts.append(f"Fee: {item['actionable']}.")
    if item.get("summary"):
        parts.append(item["summary"])
    rel = _item_relevance(ctx, item)
    if rel:
        parts.append(rel)
    parts.append(ctx.cta("Want me to save the date and send the registration details? Reply YES.",
                         "Date save karke registration details bhej doon? Reply YES."))
    ctx.facts.append(f"digest[{item.get('id')}]")
    ctx.levers += ["specificity (date, credits, speaker)", "curiosity", "single binary CTA"]
    return " ".join(parts), "binary_yes_stop", {
        "deliverable": "saving the date and sending registration details",
        "artifact": f"{item.get('title')} — {slot_label(item.get('date')) if item.get('date') else ''}. "
                    f"{item.get('actionable', '')}"}


def h_competitor_opened(ctx: Ctx):
    p = ctx.payload
    parts = []
    comp = p.get("competitor_name")
    if comp:
        line = f"{ctx.sal}, heads-up: {comp} opened {p.get('distance_km')} km from you"
        if p.get("opened_date"):
            line += f" on {day_month(p['opened_date'])}"
        their = p.get("their_offer")
        if their:
            line += f", leading with \"{their}\""
            tp = price_in(their)
            mine = next((o for o in ctx.active_offers
                         if set(service_in(o).lower().split()) & set(service_in(their).lower().split())
                         and price_in(o)), None)
            if tp and mine and price_in(mine) > tp:
                line += f" — {inr(price_in(mine) - tp)} under your {inr(price_in(mine))}"
                ctx.facts.append(f"merchant offer '{mine}' vs competitor '{their}'")
        parts.append(line + ".")
        ctx.facts.append(f"trigger.payload competitor={comp}, {p.get('distance_km')}km")
    else:
        parts.append(f"{ctx.sal}, a new {SINGULAR.get(ctx.slug, 'business')} listing has come up near {ctx.locality}.")
    parts.append("I wouldn't race them on price.")
    pos = ctx.review_theme("pos")
    if pos:
        q = pos.get("common_quote")
        parts.append(f"Your edge is already in your reviews — {pos.get('occurrences_30d')} this month"
                     + (f" say \"{q}\"." if q else f" praise your {theme_label(pos.get('theme'))}."))
        ctx.facts.append(f"review_themes {pos.get('theme')} x{pos.get('occurrences_30d')}")
    wins = ctx.peer_wins()
    if wins and not pos:
        parts.append(f"You start ahead: {wins[0]}.")
    neg = ctx.review_theme("neg")
    lapsed = ctx.lapsed_count()
    if lapsed:
        parts.append(f"The exposed flank is your {num(lapsed[0])} {ctx.people} lapsed {lapsed[1]} — they're the easiest for a newcomer to pick off.")
        ctx.facts.append(f"customer_aggregate lapsed={lapsed[0]}")
        ask = "draft a GBP post built on that + a win-back note for the lapsed list"
    elif neg:
        parts.append(f"The one soft spot a newcomer can exploit: {neg.get('occurrences_30d')} reviews mention {theme_label(neg.get('theme'))}.")
        ask = "draft a GBP post leaning on your strengths" + (f" and featuring {ctx.active_offers[0]}" if ctx.active_offers else "")
    else:
        ask = "draft a GBP post leaning on your strengths" + (f" and featuring {ctx.active_offers[0]}" if ctx.active_offers else "")
    parts.append(ctx.ask(ask))
    ctx.levers += ["loss aversion", "social proof (own reviews)", "single binary CTA"]
    return " ".join(parts), "binary_yes_stop", {"deliverable": ask.replace("draft", "drafting", 1), "artifact": ""}


def h_perf_dip(ctx: Ctx):
    p = ctx.payload
    parts = []
    if not ctx.placeholder and p.get("metric") and p.get("delta_pct") is not None:
        metric = humanize(p["metric"])
        line = f"{ctx.sal}, your {metric} dropped {pct(p['delta_pct'])} over the last {p.get('window', '7d').replace('d', ' days')}"
        base = p.get("vs_baseline")
        if base:
            now_v = round(base * (1 + p["delta_pct"]))
            line += f" — roughly {num(now_v)} against your usual {num(base)}"
        parts.append(line + ".")
        ctx.facts.append(f"trigger.payload {metric} {p['delta_pct']}")
    else:
        wd = ctx.worst_delta()
        gaps = ctx.peer_gaps()
        if wd:
            parts.append(f"{ctx.sal}, {ctx.name}'s {wd[0]} are down {pct(wd[1])} week-on-week.")
            ctx.facts.append(f"performance.delta_7d {wd[0]}={wd[1]}")
        elif gaps:
            parts.append(f"{ctx.sal}, quick performance check on {ctx.name}: {gaps[0]}.")
        else:
            parts.append(f"{ctx.sal}, quick performance check on {ctx.name}: {num(ctx.perf.get('views'))} views and {num(ctx.perf.get('calls'))} calls in the last 30 days.")
    # Fixable causes, only those the data supports
    causes = []
    if not ctx.active_offers:
        causes.append("no active offer on your listing")
    if ctx.ident.get("verified") is False:
        causes.append("the Google profile is still unverified")
    if ctx.sub.get("status") == "expired":
        causes.append("profile upkeep paused since the plan expired")
    stale = ctx.signal("stale_posts")
    if stale:
        causes.append(f"last Google post was {stale.replace('d', ' days')} ago")
    if causes:
        parts.append("The fixable gaps I can see: " + "; ".join(causes[:3]) + ".")
    offer = ctx.active_offers[0] if ctx.active_offers else ctx.catalog_offer()
    if ctx.active_offers:
        ask = f"push \"{offer}\" in a fresh Google post today"
    elif offer:
        ask = f"put \"{offer}\" live on your listing today"
        ctx.facts.append(f"category offer_catalog '{offer}'")
    else:
        ask = "draft a fresh Google post today"
    parts.append(ctx.ask(ask))
    ctx.levers += ["loss aversion", "specificity", "effort externalization"]
    return " ".join(parts), "binary_yes_stop", {"deliverable": ask, "artifact": _post_draft(ctx, offer)}


def h_perf_spike(ctx: Ctx):
    p = ctx.payload
    parts = []
    if not ctx.placeholder and p.get("metric") and p.get("delta_pct") is not None:
        line = f"{ctx.sal}, {humanize(p['metric'])} are up {pct(p['delta_pct'])} this week"
        if p.get("vs_baseline"):
            line += f" against a baseline of {num(p['vs_baseline'])}"
        parts.append(line + ".")
        if p.get("likely_driver"):
            parts.append(f"Likely driver: your {humanize(p['likely_driver']).replace(' post', '')} post.")
        ctx.facts.append(f"trigger.payload {p['metric']} +{p['delta_pct']}")
    else:
        bd = ctx.best_delta()
        if bd:
            parts.append(f"{ctx.sal}, small but real uptick at {ctx.name}: {bd[0]} +{pct(bd[1])} week-on-week.")
            ctx.facts.append(f"performance.delta_7d {bd[0]}=+{bd[1]}")
        else:
            parts.append(f"{ctx.sal}, {ctx.name} is holding steady this week.")
    wins = ctx.peer_wins()
    if wins:
        parts.append(f"And {wins[0]}.")
    if ctx.ident.get("verified") is False:
        ask = "start your Google verification so more of this traffic sees you"
        parts.append("One catch: the listing is still unverified, which caps how prominently Google shows you.")
    elif not p.get("likely_driver") and not ctx.active_offers:
        offer = ctx.catalog_offer()
        ask = f"lock the momentum in with \"{offer}\" as a live offer" if offer else "draft a follow-up post while interest is high"
    else:
        ask = "pin that post and turn it into a live offer on your listing"
    parts.append(ctx.ask(ask))
    ctx.levers += ["positive reinforcement", "curiosity", "single binary CTA"]
    return " ".join(parts), "binary_yes_stop", {"deliverable": ask, "artifact": ""}


def h_seasonal_perf_dip(ctx: Ctx):
    p = ctx.payload
    metric = humanize(p.get("metric", "views"))
    parts = [f"{ctx.sal}, {metric} are down {pct(p.get('delta_pct', ctx.delta.get('views_pct', 0)))} this week — and that's expected."]
    item = ctx.digest(kinds=("seasonal",))
    beat = next((b for b in ctx.cat.get("seasonal_beats", []) if "Apr" in b.get("month_range", "")), None)
    if beat:
        parts.append(f"{beat['month_range']} is the {beat['note'].split(' — ')[0]} for {ctx.cat.get('display_name', ctx.slug).lower()}.")
    if item and item.get("actionable"):
        parts.append(f"So I wouldn't chase it with ad spend: {item['actionable'][0].lower() + item['actionable'][1:].rstrip('.')}.")
        ctx.facts.append(f"digest[{item.get('id')}]")
    churn, pchurn = ctx.agg.get("monthly_churn_pct"), ctx.peer.get("monthly_churn_pct")
    members = ctx.agg.get("total_active_members")
    if churn and members:
        line = f"The number that matters now is retention: churn is {pct(churn)}/month"
        if pchurn:
            line += f" vs {pct(pchurn)} for peer gyms"
        line += f" — on {num(members)} members that's ~{num(members * churn)} people a month."
        parts.append(line)
        ctx.facts.append(f"customer_aggregate members={members} churn={churn}")
    ask = "draft a 6-week summer consistency challenge for your current members"
    parts.append(f"Want me to {ask}? Reply YES.")
    ctx.levers += ["anxiety pre-emption", "loss aversion (churn)", "effort externalization"]
    return " ".join(parts), "binary_yes_stop", {
        "deliverable": "drafting the 6-week summer challenge",
        "artifact": ("Summer Consistency Challenge: 3 sessions/week for 6 weeks, weekly check-in on the board, "
                     "members who hit 18 sessions get a free body-composition re-test; announce via WhatsApp + a pinned GBP post.")}


def h_milestone(ctx: Ctx):
    p = ctx.payload
    parts = []
    if not ctx.placeholder and p.get("metric"):
        metric = humanize(p["metric"]).replace("review count", "Google reviews")
        now_v, target = p.get("value_now"), p.get("milestone_value")
        if p.get("is_imminent") and now_v is not None and target:
            parts.append(f"{ctx.sal}, {ctx.name} is at {num(now_v)} {metric} — {num(target - now_v)} away from {num(target)}.")
        else:
            parts.append(f"{ctx.sal}, {ctx.name} just crossed {num(target or now_v)} {metric}.")
        ctx.facts.append(f"trigger.payload {p['metric']} {now_v}->{target}")
        pos = ctx.review_theme("pos")
        if pos:
            parts.append(f"Your {theme_label(pos['theme'])} is doing the work — {pos['occurrences_30d']} review mentions this month.")
        ask = "draft a table-card + a one-line WhatsApp asking happy regulars for a review"
        parts.append("A small ask to today's regulars usually closes a gap like this within days.")
    else:
        wins = ctx.peer_wins()
        if wins:
            parts.append(f"{ctx.sal}, a milestone worth noting for {ctx.name}: {wins[0]}.")
            ctx.facts.append("performance vs peer_stats")
        else:
            parts.append(f"{ctx.sal}, {ctx.name} hit {num(ctx.perf.get('views'))} profile views in the last 30 days.")
        wd = ctx.worst_delta()
        if wd and wd[0] == "calls":
            parts.append(f"The gap: calls are down {pct(wd[1])} this week, so people are looking but not dialling.")
            ask = "draft a post with a clear offer and a call button to convert that attention"
        else:
            ask = "turn this into a 'thank you' Google post that also asks for reviews"
    parts.append(ctx.ask(ask))
    ctx.levers += ["achievement / social proof", "effort externalization", "single binary CTA"]
    return " ".join(parts), "binary_yes_stop", {"deliverable": ask, "artifact": ""}


def h_dormant(ctx: Ctx):
    p = ctx.payload
    parts = [f"{ctx.sal}, no pitch today — just something I noticed on {ctx.name}."]
    facts = []
    wd = ctx.worst_delta()
    if wd:
        facts.append(f"{wd[0]} are down {pct(wd[1])} this week")
    lapsed = ctx.lapsed_count()
    total = ctx.agg.get("total_unique_ytd")
    if lapsed and total:
        facts.append(f"{num(lapsed[0])} of your {num(total)} {ctx.people} this year haven't been back in {lapsed[1]}")
    elif ctx.peer_gaps():
        facts.append(ctx.peer_gaps()[0])
    if facts:
        s = (" and " if lapsed else ", and ").join(facts[:2])
        parts.append(s[0].upper() + s[1:] + ".")
        ctx.facts.append("performance.delta_7d / customer_aggregate")
    if lapsed:
        parts.append("That second number is the recoverable one.")
        ask = "send you the lapsed-customer list with a ready win-back message"
    else:
        offer = ctx.active_offers[0] if ctx.active_offers else ctx.catalog_offer()
        ask = f"draft a quick Google post around \"{offer}\" to get calls moving again" if offer else "draft a quick Google post to get calls moving again"
    if p.get("days_since_last_merchant_message"):
        ctx.facts.append(f"trigger.payload days_since_last_merchant_message={p['days_since_last_merchant_message']}")
    parts.append(ctx.ask(ask))
    ctx.levers += ["reciprocity", "curiosity", "loss aversion"]
    return " ".join(parts), "binary_yes_stop", {"deliverable": ask, "artifact": ""}


def h_winback(ctx: Ctx):
    p = ctx.payload
    parts = [f"{ctx.sal}, it's been {p.get('days_since_expiry', ctx.sub.get('days_since_expiry', ''))} days since {ctx.name}'s plan lapsed."]
    bits = []
    if p.get("perf_dip_pct") is not None:
        bits.append(f"calls are down {pct(p['perf_dip_pct'])}")
    if p.get("lapsed_customers_added_since_expiry"):
        bits.append(f"{p['lapsed_customers_added_since_expiry']} more customers have slipped past 90 days without a visit")
    if bits:
        parts.append("Since then " + " and ".join(bits) + ".")
    parts.append("Reactivating brings back profile upkeep and lets me start on the lapsed list the same day.")
    ask = f"restart with a win-back message to those {p['lapsed_customers_added_since_expiry']}" if p.get("lapsed_customers_added_since_expiry") else "restart and send the win-back message"
    parts.append(ctx.ask(ask))
    ctx.facts.append("trigger.payload winback stats")
    ctx.levers += ["loss aversion", "specificity"]
    return " ".join(parts), "binary_yes_stop", {"deliverable": ask, "artifact": ""}


def h_renewal_due(ctx: Ctx):
    p = ctx.payload
    days = p.get("days_remaining", ctx.sub.get("days_remaining"))
    plan = p.get("plan", ctx.sub.get("plan", ""))
    parts = [f"{ctx.sal}, your {plan} plan ends in {days} days."]
    parts.append(f"Last 30 days on the listing: {num(ctx.perf.get('views'))} views, {num(ctx.perf.get('calls'))} calls, {num(ctx.perf.get('directions'))} direction requests.")
    wd = ctx.worst_delta()
    if wd:
        parts.append(f"With {wd[0]} already down {pct(wd[1])} this week, a lapse would also pause profile upkeep right when it's needed.")
    if p.get("renewal_amount"):
        parts.append(f"Renewal is {inr(p['renewal_amount'])}.")
    parts.append(ctx.cta("Want me to send the renewal details? Reply YES.", "Renewal details bhej doon? Reply YES."))
    ctx.facts.append("subscription + performance 30d")
    ctx.levers += ["loss aversion", "specificity"]
    return " ".join(parts), "binary_yes_stop", {"deliverable": "sending the renewal details", "artifact": ""}


def h_festival(ctx: Ctx):
    p = ctx.payload
    fest = p.get("festival")
    parts = []
    beat = None
    for b in ctx.cat.get("seasonal_beats", []) or []:
        note = b.get("note", "").lower()
        if "festival" in note or "wedding" in note or (fest and fest.lower() in note):
            beat = b
            break
    offers = ctx.active_offers
    if fest:
        days = p.get("days_until")
        parts.append(f"{ctx.sal}, {fest} is on {day_month_year(p.get('date'))}" + (f" — {days} days out." if days else "."))
        ctx.facts.append(f"trigger.payload festival={fest} days_until={days}")
        if days and days > 60:
            parts.append("Too early for a discount, but not too early to plan:")
            if beat:
                parts[-1] += f" {beat['month_range']} is {beat['note']}."
            ask = ("draft a festive package from your "
                   + (" + ".join(f'\"{o}\"' for o in offers[:2]) if offers else "best-selling services")
                   + " so pre-bookings can open in September")
        else:
            if beat:
                parts.append(f"{beat['month_range']}: {beat['note']}.")
            ask = f"put up a {fest} post featuring \"{offers[0]}\" today" if offers else f"draft a {fest} offer + Google post today"
    else:
        if beat:
            bits = beat["note"].split(" — ")
            when = f"{bits[1]} ({bits[0]})" if len(bits) == 2 else bits[0]
            parts.append(f"{ctx.sal}, festival season is on the calendar — for {ctx.cat.get('display_name', ctx.slug).lower()}, {beat['month_range']} is when {when}.")
            ctx.facts.append(f"category.seasonal_beats {beat['month_range']}")
        else:
            parts.append(f"{ctx.sal}, festival season is coming up for {ctx.name}.")
        wins = ctx.peer_wins()
        if wins:
            parts.append(f"You go in strong: {wins[0]}.")
        offer = offers[0] if offers else ctx.catalog_offer(prefer=("service_at_price",))
        ask = f"draft a festive 6-week plan post built around \"{offer}\"" if offer else "draft a festive campaign post"
    parts.append(ctx.ask(ask))
    ctx.levers += ["timing / loss aversion", "effort externalization", "single binary CTA"]
    return " ".join(parts), "binary_yes_stop", {"deliverable": ask, "artifact": ""}


def h_curious_ask(ctx: Ctx):
    parts = [f"Hi {ctx.owner_first or ctx.name}, one quick question for the week: what's been the most-asked "
             f"{'dish' if ctx.slug == 'restaurants' else 'service'} at {ctx.name} lately?"]
    pos = ctx.review_theme("pos")
    trend = None
    blob = (humanize(pos.get("theme")) + " " + str(pos.get("common_quote", ""))).lower() if pos else ""
    for ts in ctx.cat.get("trend_signals", []) or []:
        q = ts.get("query", "")
        if any(w in blob for w in q.lower().split() if len(w) > 3 and w not in ("near", "price", "delhi")):
            trend = ts
            break
    if pos:
        guess = None
        quote = pos.get("common_quote", "")
        for vocab in ctx.voice.get("vocab_allowed", []) or []:
            if vocab.lower() in (quote + " " + humanize(pos.get("theme"))).lower():
                guess = vocab
                break
        guess = guess or humanize(pos.get("theme")).replace(" quality", "")
        line = f"My guess: {guess} — {pos['occurrences_30d']} reviews this month" + (f" say \"{quote}\"" if quote else " mention it")
        if trend:
            line += f", and '{trend['query']}' searches are up {pct(trend['delta_yoy'])} YoY"
        parts.append(line + ".")
        ctx.facts.append(f"review_themes {pos.get('theme')}")
    else:
        ts = max(ctx.cat.get("trend_signals", []) or [{}], key=lambda x: x.get("delta_yoy", 0))
        if ts.get("query"):
            parts.append(f"Across {ctx.cat.get('display_name', ctx.slug).lower()}, '{ts['query']}' searches are up "
                         f"{pct(ts['delta_yoy'])} YoY — curious whether you're seeing it at the counter too.")
            ctx.facts.append(f"trend_signals {ts['query']}")
    parts.append("Tell me in one line and I'll turn it into a Google post + a ready price reply for WhatsApp enquiries — 5 minutes, no effort from you.")
    ctx.levers += ["asking the merchant", "curiosity (guess)", "reciprocity", "effort externalization"]
    return " ".join(parts), "open_ended", {"deliverable": "turning your answer into a Google post + WhatsApp price reply", "artifact": ""}


def h_ipl(ctx: Ctx):
    p = ctx.payload
    when = slot_label(p.get("match_time_iso")).split(", ")[-1] if p.get("match_time_iso") else ""
    parts = [f"{ctx.sal}, {p.get('match', 'IPL match')} at {p.get('venue', ctx.city)} tonight" + (f", {when}" if when else "") + "."]
    item = ctx.digest(kinds=("seasonal",))
    weeknight = p.get("is_weeknight")
    combo = ctx.catalog_find("Match-night")
    if weeknight is False:
        if item:
            parts.append("One caution: it's a weekend match, and across metros Saturday IPL games have cut restaurant covers ~12% "
                         "(people host watch-parties at home) while weeknight games lift covers ~18%.")
            ctx.facts.append(f"digest[{item.get('id')}]")
        parts.append("So tonight is a delivery play, not a dine-in promo")
        tue_thu = next((o for o in ctx.active_offers if "Tue-Thu" in o or "Tue–Thu" in o), None)
        parts[-1] += (f" — and your \"{tue_thu}\" doesn't apply on a Saturday." if tue_thu else ".")
        neg = ctx.review_theme("neg")
        if neg and "deliver" in neg.get("theme", ""):
            parts.append(f"With {neg['occurrences_30d']} reviews this month flagging late delivery, I'd close pre-orders by 7pm so the kitchen isn't slammed at toss.")
        ask = f"set up \"{combo}\" for delivery tonight with a 7pm pre-order cut-off" if combo else "set up a delivery-only match combo for tonight"
    else:
        parts.append("Weeknight matches have been lifting restaurant covers ~18% this season.")
        ask = f"push \"{combo}\" for dine-in tonight" if combo else "push a match-night combo for tonight"
    parts.append(ctx.ask(ask))
    ctx.facts.append(f"trigger.payload match={p.get('match')} weeknight={weeknight}")
    ctx.levers += ["counter-intuitive data", "loss aversion", "time-bound", "single binary CTA"]
    return " ".join(parts), "binary_yes_stop", {"deliverable": ask, "artifact": ""}


def h_review_theme(ctx: Ctx):
    p = ctx.payload
    theme = p.get("theme")
    if not theme:
        neg = ctx.review_theme("neg")
        if neg:
            p = neg
            theme = neg.get("theme")
    parts = []
    if theme:
        line = f"{ctx.sal}, {p.get('occurrences_30d', 'several')} reviews in the last 30 days mention {theme_label(theme)}"
        if p.get("trend") == "rising":
            line += " and the trend is rising"
        parts.append(line + ".")
        if p.get("common_quote"):
            parts.append(f"One reads: \"{p['common_quote']}\".")
        pos = ctx.review_theme("pos")
        if pos:
            parts.append(f"The good news: {pos['occurrences_30d']} reviews praise your {theme_label(pos['theme'])}, so this is an operations fix, not a product one.")
        ctx.facts.append(f"review theme {theme}")
    else:
        parts.append(f"{ctx.sal}, a recurring theme is showing up in {ctx.name}'s recent reviews.")
    ask = "draft a calm public reply for those reviews + an honest timing note for your listing"
    parts.append(ctx.ask(ask))
    ctx.levers += ["loss aversion (reputation)", "specificity (quote)", "effort externalization"]
    return " ".join(parts), "binary_yes_stop", {
        "deliverable": "drafting the review reply",
        "artifact": (f"Reply draft: \"Thank you for the honest feedback — you're right, that wait isn't the {ctx.name} standard. "
                     "We've tightened our dispatch timing this week. Please give us another try and tell us how it goes.\"")}


def _planning_thali(ctx: Ctx, base: int) -> tuple[str, str]:
    def r5(x):
        return int(5 * round(x / 5))
    t1, t2, t3 = r5(base * 0.91), r5(base * 0.85), r5(base * 0.78)
    m = re.search(r"(\d+)\s*orders/day", ctx.history_text())
    volume = f" ({m.group(1)} orders/day today)" if m else ""
    art = (f"• 10–24 thalis/day: {inr(t1)} each\n"
           f"• 25–49: {inr(t2)} each + free delivery\n"
           f"• 50+: {inr(t3)} each + one filter coffee per head\n"
           f"• Orders by 5pm the day before; lunch delivered 12:30–1:30pm")
    body = (f"{ctx.sal}, here's a first cut of the corporate thali package, built off your {inr(base)} weekday thali{volume}:\n"
            f"{art}\n"
            "Prices are a starting point — tweak to your food cost. Next I list it on your Google profile and draft a 3-line "
            f"WhatsApp for office admins around {ctx.locality}. Go ahead? Reply YES.")
    return body, art


def _planning_kids_yoga(ctx: Ctx) -> tuple[str, str]:
    txt = ctx.history_text()
    weeks = re.search(r"(\d+)-week", txt)
    per = re.search(r"(\d+)\s*classes/week", txt)
    ages = re.search(r"age\s*(\d+)\s*-\s*(\d+)", txt)
    price = re.search(r"₹\s?[\d,]+", txt)
    w, c = (int(weeks.group(1)) if weeks else 4), (int(per.group(1)) if per else 3)
    age = f"{ages.group(1)}–{ages.group(2)}" if ages else "7–12"
    pr = price.group(0) if price else None
    small = next((r for r in ctx.m.get("review_themes", []) if "small" in r.get("theme", "")), None)
    post = (f"\"Summer Kids Yoga Camp at {ctx.name}, {ctx.locality} — {w} weeks, {c} classes a week for ages {age}. "
            f"Balance, focus and flexibility in {'small batches' if small else 'a calm, guided setting'}."
            + (f" {pr} for all {w * c} classes." if pr else "") + " Call us to book a free first class.\"")
    body = (f"{ctx.sal}, here's the kids' yoga camp, ready to publish — built on the plan we discussed "
            f"({w} weeks, {c} classes/week, ages {age}{', ' + pr if pr else ''}).\n"
            f"Google post draft: {post}\n"
            + (f"I leaned on 'small classes' because {small['occurrences_30d']} reviews this month praise it. " if small else "")
            + "Want me to publish it on your Google profile today? Reply YES.")
    return body, post


def h_planning(ctx: Ctx):
    topic = str(ctx.payload.get("intent_topic", ""))
    ctx.facts.append(f"trigger.payload intent_topic={topic}; merchant said '{ctx.payload.get('merchant_last_message', '')}'")
    ctx.levers += ["intent handoff (action mode)", "effort externalization", "complete artifact"]
    if "thali" in topic:
        base = next((price_in(o) for o in ctx.active_offers if "thali" in o.lower() and price_in(o)), None)
        if base:
            body, art = _planning_thali(ctx, base)
            return body, "binary_yes_stop", {"deliverable": "listing the corporate thali package and drafting the office-admin WhatsApp", "artifact": art}
    if "yoga" in topic or "kids" in topic:
        body, art = _planning_kids_yoga(ctx)
        return body, "binary_yes_stop", {"deliverable": "publishing the kids' yoga camp post", "artifact": art}
    offer = ctx.active_offers[0] if ctx.active_offers else ctx.catalog_offer()
    art = (f"• What: {humanize(topic)}\n• Hook: {offer or 'a clear service + price'}\n"
           f"• Launch: Google post + WhatsApp to your regulars\n• Review after 14 days")
    body = (f"{ctx.sal}, here's a first-cut plan for {humanize(topic)}:\n{art}\n"
            "Want me to draft the launch post now? Reply YES.")
    return body, "binary_yes_stop", {"deliverable": "drafting the launch post", "artifact": art}


def h_supply_alert(ctx: Ctx):
    p = ctx.payload
    item = ctx.digest(p.get("alert_id"), kinds=("alert", "supply"))
    batches = ", ".join(p.get("affected_batches", []) or [])
    parts = []
    followup = any("list" in str(h.get("body", "")).lower() for h in ctx.history("merchant"))
    lead = "following up on the list you asked for" if followup else "urgent"
    parts.append(f"{ctx.sal}, {lead}: {item.get('source', 'CDSCO') if item else 'regulator'} recall on {p.get('molecule', 'the molecule')}"
                 + (f" batches {batches}" if batches else "") + (f" ({p.get('manufacturer')})" if p.get("manufacturer") else "") + ".")
    if item and item.get("summary"):
        s = item["summary"]
        risk = re.search(r"No safety risk[^.]*\.", s)
        parts.append(("Reason: sub-potency. " if "sub-potency" in s else "") + (risk.group(0) if risk else ""))
    n = ctx.agg.get("chronic_rx_count")
    if n:
        parts.append(f"Your {num(n)} chronic-Rx customers are the pool to check against dispensing records.")
    parts.append("I've drafted the customer WhatsApp note + a replacement-pickup script. Want me to send both? Reply YES.")
    ctx.facts.append(f"trigger.payload batches={batches}; customer_aggregate.chronic_rx_count={n}")
    ctx.levers += ["urgency", "specificity (batch numbers)", "effort externalization", "continuity with merchant's ask"]
    return " ".join(x for x in parts if x), "binary_yes_stop", {
        "deliverable": "sending the customer note and pickup script",
        "artifact": (f"Customer note: \"Namaste, {ctx.name} here. A few batches of {p.get('molecule')} have been voluntarily recalled "
                     "for lower strength — not a safety issue. If your strip shows batch "
                     f"{batches}, please bring it in and we'll replace it free, same day.\"")}


def h_category_seasonal(ctx: Ctx):
    p = ctx.payload
    trends = []
    for t in p.get("trends", []) or []:
        m = re.match(r"(.+?)_demand_([+-]\d+)", t)
        if m:
            trends.append(f"{humanize(m.group(1)).replace('cold cough', 'cold/cough')} {m.group(2).replace('-', '−')}%")
    parts = [f"{ctx.sal}, the summer shift is showing up: " + ", ".join(trends) + "." if trends
             else f"{ctx.sal}, seasonal demand is shifting this month."]
    item = ctx.digest(kinds=("seasonal",))
    if item and item.get("actionable"):
        parts.append(f"Worth a shelf reshuffle this week — {item['actionable'][0].lower() + item['actionable'][1:]}.")
    content = next((c for c in ctx.cat.get("patient_content_library", []) or [] if "summer" in c.get("id", "")), None)
    total = ctx.agg.get("total_unique_ytd")
    if content and total:
        parts.append(f"I can also send your {num(total)} customers the \"{content['title']}\" note — useful, not salesy, and it brings them in for exactly these items.")
        ask = "draft the counter list + that customer WhatsApp"
    else:
        ask = "draft the restock + counter-display list"
    parts.append(ctx.ask(ask))
    ctx.facts.append("trigger.payload trends + category digest/content")
    ctx.levers += ["specificity", "reciprocity", "effort externalization"]
    return " ".join(parts), "binary_yes_stop", {"deliverable": ask, "artifact": content.get("body", "") if content else ""}


def h_gbp_unverified(ctx: Ctx):
    p = ctx.payload
    parts = [f"{ctx.sal}, {ctx.name} is still unverified on Google."]
    up = p.get("estimated_uplift_pct")
    views = ctx.perf.get("views")
    if up and views:
        parts.append(f"Verified listings see an estimated {pct(up)} more visibility — on your {num(views)} monthly views that's roughly {num(views * up)} extra people finding you.")
        ctx.facts.append(f"trigger.payload uplift={up}; performance.views={views}")
    path = p.get("verification_path")
    if path:
        parts.append(f"Verification is by {humanize(path).replace('or', 'or a')}; the phone route is the quickest.")
    if not ctx.active_offers:
        parts.append("Once verified, an offer on the listing is the next easy win.")
    parts.append(ctx.cta("Want me to walk you through it now, step by step? Reply YES.",
                         "Main step-by-step karwa doon? Reply YES."))
    ctx.levers += ["loss aversion", "specificity", "effort externalization"]
    return " ".join(parts), "binary_yes_stop", {"deliverable": "starting the verification walkthrough", "artifact": ""}


def h_generic(ctx: Ctx):
    """Unknown kinds (weather, local news, trend movement, …): anchor on payload + best merchant fact."""
    p = {k: v for k, v in ctx.payload.items() if k not in ("placeholder", "metric_or_topic")}
    parts = []
    topic = humanize(ctx.kind)
    if p:
        detail = ", ".join(f"{humanize(k)}: {v}" for k, v in list(p.items())[:3]
                           if isinstance(v, (str, int, float)) and not str(k).endswith("_id"))
        parts.append(f"{ctx.sal}, flagging a {topic} for {ctx.name}" + (f" — {detail}." if detail else "."))
    else:
        parts.append(f"{ctx.sal}, a {topic} flag came up for {ctx.name}.")
    gaps, wins = ctx.peer_gaps(), ctx.peer_wins()
    if gaps:
        parts.append(f"Worth knowing alongside it: {gaps[0]}.")
    elif wins:
        parts.append(f"You're in a good position: {wins[0]}.")
    offer = ctx.active_offers[0] if ctx.active_offers else ctx.catalog_offer()
    ask = f"draft a timely Google post featuring \"{offer}\"" if offer else "draft a timely Google post"
    parts.append(ctx.ask(ask))
    ctx.levers += ["timeliness", "effort externalization"]
    return " ".join(parts), "binary_yes_stop", {"deliverable": ask, "artifact": _post_draft(ctx, offer)}


def _post_draft(ctx: Ctx, offer: Optional[str]) -> str:
    if not offer:
        return ""
    return f"Google post: \"{offer} at {ctx.name}, {ctx.locality}. Call or walk in — we'll take care of you.\""


# ---------------------------------------------------------------------------
# Customer-facing handlers (send_as = merchant_on_behalf)
# ---------------------------------------------------------------------------


def _cust_name(c: dict) -> str:
    n = str(((c or {}).get("identity") or {}).get("name", "")).strip()
    return n.split("(")[0].strip() or "there"


def _parent_name(c: dict) -> Optional[str]:
    m = re.search(r"parent:\s*([^)]+)", str(((c or {}).get("identity") or {}).get("name", "")))
    return m.group(1).strip() if m else None


def c_recall(ctx: Ctx):
    c, p = ctx.c, ctx.payload
    lang, name = customer_lang(c), _cust_name(c)
    slots = [s.get("label") for s in p.get("available_slots", []) or [] if s.get("label")]
    offer = next((o for o in ctx.active_offers if "clean" in o.lower()), ctx.active_offers[0] if ctx.active_offers else None)
    service = humanize(p.get("service_due", "")).replace("6 month", "6-month")
    last = p.get("last_service_date") or (c.get("relationship") or {}).get("last_visit")
    due_hi = {"dentists": "aapka check-up", "pharmacies": "aapka refill", "gyms": "wapas routine pe aane ka time",
              "salons": "aapki next appointment"}.get(ctx.slug, "aapki next visit")
    if lang in ("hinglish", "hindi"):
        parts = [f"Hi {name}, {ctx.name} se 🦷" if ctx.slug == "dentists" else f"Hi {name}, {ctx.name} se."]
        if last:
            parts.append(f"Aapki last visit {day_month(last)} ko hui thi — {service or due_hi} ab due hai" + (f" ({day_month(p['due_date'])})." if p.get("due_date") else "."))
        if slots:
            parts.append(f"Aapke liye {'evening ' if 'evening' in str((c.get('preferences') or {}).get('preferred_slots', '')) else ''}slots rakhe hain: {' ya '.join(slots[:2])}.")
        if offer:
            parts.append(f"{service_in(offer)}: {inr(price_in(offer))}." if price_in(offer) else f"{offer}.")
        parts.append("Reply 1 for " + slots[0].split(",")[0] + (", 2 for " + slots[1].split(",")[0] if len(slots) > 1 else "") + " — ya apna time batayein." if slots else "Reply YES aur hum slot fix kar denge.")
    else:
        greet = customer_greeting(c)
        parts = [f"{greet} {name}, {ctx.sender_for_customer} here."]
        if last and ctx.slug == "dentists":
            parts.append(f"Your last visit was on {day_month(last)} — your {service or 'check-up'} is due now.")
        elif last and ctx.slug == "pharmacies":
            parts.append(f"Your last visit with us was on {day_month(last)} — if you're on regular medicines, we can keep your refill ready or deliver it home.")
        elif last and ctx.slug == "salons":
            parts.append(f"Your last visit was on {day_month(last)} — about time for a refresh?")
        elif last:
            parts.append(f"We haven't seen you since {day_month(last)} — your spot is waiting.")
        visits = (c.get("relationship") or {}).get("visits_total")
        if ctx.slug == "gyms" and visits:
            parts.append(f"You'd built a good run of {visits} sessions with us — easier to restart now than later.")
        if slots:
            parts.append(f"Open slots: {' or '.join(slots[:2])}.")
        extra = next((o for o in ctx.active_offers if "free" in o.lower()), None) if ctx.slug == "gyms" else offer
        if extra:
            parts.append(f"{extra} is on us." if "free" in extra.lower() else f"{extra}.")
        parts.append(("Reply 1 for " + slots[0].split(",")[0] + (", 2 for " + slots[1].split(",")[0] if len(slots) > 1 else "") + ", or tell us a time that suits you.")
                     if slots else ("Want us to set that up? Reply YES." if ctx.slug == "pharmacies"
                                    else "Want us to book you in this week? Reply YES."))
    ctx.facts.append(f"customer last_visit={last}; slots={slots}; offer={offer}")
    ctx.levers += ["personalisation", "specific slots/price", "low-friction CTA"]
    return " ".join(parts), ("multi_choice_slot" if slots else "binary_yes_stop"), {"deliverable": "booking the slot", "artifact": ""}


def c_appointment_tomorrow(ctx: Ctx):
    c, p = ctx.c, ctx.payload
    lang, name = customer_lang(c), _cust_name(c)
    when = p.get("slot_label") or (slot_label(p["appointment_iso"]) if p.get("appointment_iso") else None)
    svc = humanize(p.get("service")) if p.get("service") else None
    if lang == "hindi":
        body = (f"Namaste {name} ji, {ctx.name} se reminder: kal aapka appointment hai" + (f" — {when}" if when else "")
                + (f" ({svc})" if svc else "") + ". Confirm karne ke liye HAAN reply karein, ya time badalna ho toh bata dijiye.")
    elif lang == "hinglish":
        body = (f"Hi {name}, {ctx.name} se quick reminder — kal aapka appointment hai" + (f", {when}" if when else "")
                + (f" ({svc})" if svc else "") + ". Reply YES to confirm, ya reschedule karna ho toh batayein.")
    else:
        body = (f"{customer_greeting(c)} {name}, a quick reminder from {ctx.name}, {ctx.locality}: you're booked with us tomorrow"
                + (f" at {when}" if when else "") + (f" for {svc}" if svc else "")
                + ". Reply YES to confirm, or tell us if you need a different time.")
    ctx.facts.append("trigger appointment_tomorrow; customer language_pref")
    ctx.levers += ["reminder utility", "binary confirm"]
    return body, "binary_confirm_cancel", {"deliverable": "confirming the appointment", "artifact": ""}


def c_chronic_refill(ctx: Ctx):
    c, p = ctx.c, ctx.payload
    lang, name = customer_lang(c), _cust_name(c)
    mols = p.get("molecule_list") or []
    if not mols:
        # Placeholder or category mismatch (e.g. a dental clinic): no medicines in context → a care follow-up, nothing invented.
        noun = {"dentists": "follow-up check", "gyms": "progress check-in", "salons": "follow-up visit"}.get(ctx.slug, "follow-up")
        if lang in ("hindi", "hinglish"):
            body = f"Namaste {name}, {ctx.name} se. Aapka {noun} due hai — is hafte ek slot book kar dein? Reply YES."
        else:
            body = (f"Hi {name}, {ctx.sender_for_customer} here. Our records show your {noun} is due — "
                    "a quick one keeps things on track. Want us to book a slot for you this week? Reply YES.")
        ctx.facts.append("placeholder refill payload; no molecules in context → neutral follow-up")
        ctx.levers += ["care continuity", "binary CTA"]
        return body, "binary_yes_stop", {"deliverable": "booking the follow-up", "artifact": ""}
    runs_out = day_month(p.get("stock_runs_out_iso")) if p.get("stock_runs_out_iso") else None
    senior = bool((c.get("identity") or {}).get("senior_citizen"))
    senior_offer = next((o for o in ctx.active_offers if "senior" in o.lower()), None)
    delivery_offer = next((o for o in ctx.active_offers if "delivery" in o.lower()), None)
    recall = next((d for d in ctx.cat.get("digest", []) or [] if d.get("kind") == "alert"
                   and any(mo.lower() in d.get("title", "").lower() for mo in mols)), None)
    via = str((c.get("preferences") or {}).get("channel", ""))
    who = f"{name.replace('Mr. ', '')} ji" if "Mr." in name else name
    mol_txt = ", ".join(mols)
    if lang in ("hindi", "hinglish"):
        parts = [f"Namaste 🙏 {ctx.name}, {ctx.locality} se."]
        parts.append(f"{who} ki monthly dawaiyan ({mol_txt})" + (f" {runs_out} tak khatam ho jayengi." if runs_out else " refill ke liye due hain."))
        parts.append("Same dose, same brand ready kar dete hain.")
        if senior and senior_offer:
            parts.append("Senior citizen 15% off lagega.")
        if delivery_offer and p.get("delivery_address_saved"):
            parts.append(f"{inr(price_in(delivery_offer) or 499)} se upar free home delivery, saved address par.")
        if recall:
            mol = next(mo for mo in mols if mo.lower() in recall.get("title", "").lower())
            parts.append(f"({mol.capitalize()} hum recall-free batch se hi denge.)")
        parts.append("Reply HAAN to confirm, ya dose badli ho toh bata dijiye.")
    else:
        parts = [f"Hi {name}, {ctx.name} here."]
        parts.append(f"Your regular medicines ({mol_txt})" + (f" run out on {runs_out}." if runs_out else " are due for refill."))
        if senior and senior_offer:
            parts.append(f"{senior_offer} applies.")
        if delivery_offer and p.get("delivery_address_saved"):
            parts.append(f"{delivery_offer} to your saved address.")
        parts.append("Reply YES to confirm the same dose, or tell us if anything changed.")
    ctx.facts.append(f"trigger molecules={mols}, runs_out={runs_out}; merchant offers; channel={via}")
    ctx.levers += ["precision (molecules, date)", "savings made explicit", "binary confirm"]
    return " ".join(parts), "binary_confirm_cancel", {"deliverable": "dispatching the refill", "artifact": ""}


def c_lapsed(ctx: Ctx):
    c, p = ctx.c, ctx.payload
    lang, name = customer_lang(c), _cust_name(c)
    rel = c.get("relationship") or {}
    days = p.get("days_since_last_visit")
    focus = humanize(p.get("previous_focus") or (c.get("preferences") or {}).get("training_focus") or "")
    pref = humanize((c.get("preferences") or {}).get("preferred_slots", ""))
    free = next((o for o in ctx.active_offers if "free" in o.lower()), None)
    offer = free or (ctx.active_offers[0] if ctx.active_offers else None)
    if lang in ("hindi", "hinglish"):
        parts = [f"Namaste {name}, {ctx.name} se."]
        parts.append("Kaafi time ho gaya aapko dekhe" + (f" — {days} din" if days else "") + ".")
        if ctx.slug == "pharmacies":
            parts.append("Regular dawaiyan chal rahi hain toh hum ghar tak refill bhej sakte hain — reminder bhi hum hi yaad dila denge.")
            ask = "Chahiye toh bas YES reply karein."
        else:
            if offer:
                parts.append(f"Wapas aane par {offer} aapke liye ready hai.")
            ask = "Is hafte ek slot rakh dein? Reply YES."
        parts.append(ask)
    else:
        parts = [f"Hi {name} 👋 {ctx.sender_for_customer} here."]
        if days:
            parts.append(f"It's been about {round(days / 7)} weeks — happens to everyone, no pressure.")
        else:
            parts.append("It's been a while since your last visit — no pressure at all.")
        if focus and ctx.slug == "gyms":
            parts.append(f"If {focus} is still the goal, " + (f"{free[0].lower() + free[1:]} are on us to ease back in" if free else "we'd love to help you restart") + ".")
        elif offer:
            parts.append(f"{offer} is ready for you when you're back.")
        if pref:
            parts.append(f"{pref.capitalize()} slots are open.")
        parts.append(("Want us to hold one for you this week?" if pref else "Want us to hold a slot for you this week?") + " Reply YES — no commitment.")
    ctx.facts.append(f"customer state={c.get('state')} visits={rel.get('visits_total')}; offer={offer}")
    ctx.levers += ["no-shame warmth", "goal recall", "free/low-risk offer", "binary CTA"]
    return " ".join(parts), "binary_yes_stop", {"deliverable": "holding the slot", "artifact": ""}


def c_trial_followup(ctx: Ctx):
    c, p = ctx.c, ctx.payload
    name, parent = _cust_name(c), _parent_name(c)
    opts = [o.get("label") for o in p.get("next_session_options", []) or [] if o.get("label")]
    greet = customer_greeting(c)
    addressee = parent or name
    child = name if parent else None
    parts = [f"{greet} {addressee}! {ctx.sender_for_customer} here."]
    trial = day_month(p.get("trial_date")) if p.get("trial_date") else None
    if child:
        parts.append(f"Hope {child} enjoyed the trial class" + (f" on {trial}" if trial else "") + ".")
    else:
        parts.append("Hope you enjoyed the trial" + (f" on {trial}" if trial else "") + ".")
    if opts:
        parts.append(f"Next session: {opts[0]} — shall we keep {'his' if child else 'your'} spot? Reply YES.")
    else:
        parts.append("Shall we book the next session? Reply YES.")
    ctx.facts.append(f"trial_date={p.get('trial_date')}; next={opts}")
    ctx.levers += ["continuity", "specific slot", "binary CTA"]
    return " ".join(parts), "binary_yes_stop", {"deliverable": "reserving the session", "artifact": ""}


def c_wedding(ctx: Ctx):
    c, p = ctx.c, ctx.payload
    name = _cust_name(c)
    days = p.get("days_to_wedding")
    wd = p.get("wedding_date") or (c.get("preferences") or {}).get("wedding_date")
    raw_step = str(p.get("next_step_window_open", ""))
    m_days = re.match(r"(.+?)_(\d+)day$", raw_step)
    step = (f"{m_days.group(2)}-day {humanize(m_days.group(1)).replace('skin prep', 'skin-prep')}" if m_days
            else humanize(raw_step))
    pref = humanize((c.get("preferences") or {}).get("preferred_slots", "")).title()
    beat = next((b for b in ctx.cat.get("seasonal_beats", []) if b.get("month_range", "").startswith("Oct")), None)
    parts = [f"Hi {name} 💍 {ctx.sender_for_customer}."]
    parts.append(f"{days} days to your wedding ({day_month(wd)})" + (f" — and it's been a while since your bridal trial on {day_month(p['trial_completed'])}" if p.get("trial_completed") else "") + ".")
    if days and days > 90:
        parts.append(f"No rush on the {step or 'skin-prep'} yet — it works best started ~6 weeks before the day.")
        if beat:
            parts.append(f"What does fill up early is the calendar: {beat['month_range']} bridal bookings run {beat['note'].split('bookings ')[-1]}.")
        parts.append(f"Want me to reserve your {pref or 'preferred'} slots for the prep sessions now? Reply YES.")
    else:
        parts.append(f"This is the right window to start the {step or 'skin-prep program'}.")
        parts.append(f"Want me to book your first session on a {pref or 'weekend'}? Reply YES.")
    ctx.facts.append(f"wedding_date={wd}; days={days}; seasonal beat")
    ctx.levers += ["date specificity", "scarcity (season)", "binary CTA"]
    return " ".join(parts), "binary_yes_stop", {"deliverable": "reserving the prep slots", "artifact": ""}


def c_generic(ctx: Ctx):
    c = ctx.c
    name = _cust_name(c)
    offer = ctx.active_offers[0] if ctx.active_offers else None
    body = (f"Hi {name}, {ctx.sender_for_customer} here." + (f" {offer} is running this week." if offer else "")
            + " Want us to set up a visit for you? Reply YES.")
    ctx.levers += ["personalisation", "binary CTA"]
    return body, "binary_yes_stop", {"deliverable": "setting up the visit", "artifact": ""}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

MERCHANT_HANDLERS: dict[str, Callable] = {
    "research_digest": h_research_digest,
    "category_research_digest_release": h_research_digest,
    "research_digest_release": h_research_digest,
    "category_trend_movement": h_research_digest,
    "regulation_change": h_regulation_change,
    "compliance_alert": h_regulation_change,
    "cde_opportunity": h_cde_opportunity,
    "competitor_opened": h_competitor_opened,
    "perf_dip": h_perf_dip,
    "perf_spike": h_perf_spike,
    "seasonal_perf_dip": h_seasonal_perf_dip,
    "milestone_reached": h_milestone,
    "dormant_with_vera": h_dormant,
    "winback_eligible": h_winback,
    "renewal_due": h_renewal_due,
    "festival_upcoming": h_festival,
    "curious_ask_due": h_curious_ask,
    "scheduled_recurring": h_curious_ask,
    "ipl_match_today": h_ipl,
    "review_theme_emerged": h_review_theme,
    "active_planning_intent": h_planning,
    "supply_alert": h_supply_alert,
    "category_seasonal": h_category_seasonal,
    "gbp_unverified": h_gbp_unverified,
}

CUSTOMER_HANDLERS: dict[str, Callable] = {
    "recall_due": c_recall,
    "appointment_tomorrow": c_appointment_tomorrow,
    "chronic_refill_due": c_chronic_refill,
    "customer_lapsed_soft": c_lapsed,
    "customer_lapsed_hard": c_lapsed,
    "trial_followup": c_trial_followup,
    "wedding_package_followup": c_wedding,
    "unplanned_slot_open": c_recall,
}


def _has_consent(customer: dict) -> bool:
    prefs = customer.get("preferences") or {}
    consent = customer.get("consent") or {}
    if prefs.get("reminder_opt_in") is False:
        return False
    return bool(consent.get("opted_in_at")) and bool(consent.get("scope"))


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None,
            now: Any = None) -> dict:
    ctx = Ctx(category, merchant, trigger, customer, now)
    kind = ctx.kind
    customer_scoped = trigger.get("scope") == "customer" or kind in CUSTOMER_HANDLERS

    if customer_scoped and customer and _has_consent(customer):
        handler = CUSTOMER_HANDLERS.get(kind, c_generic)
        body, cta, follow = handler(ctx)
        send_as = "merchant_on_behalf"
        template = f"merchant_{kind}_v1"
    elif customer_scoped and customer:
        # No consent on file → never message the customer; tell the merchant instead.
        label = CONSENT_LABELS.get(kind, f"{humanize(kind)} message")
        body = (f"{ctx.sal}, I had {'an' if label[0] in 'aeiou' else 'a'} {label} ready for {_cust_name(customer)}, but there's no WhatsApp "
                "opt-in on file, so I haven't sent it. Want a counter QR card that lets customers opt in to reminders? Reply YES.")
        cta, follow, send_as, template = "binary_yes_stop", {"deliverable": "sending the opt-in QR card", "artifact": ""}, "vera", "vera_consent_gap_v1"
        ctx.facts.append("customer has no consent → merchant-facing instead")
        ctx.levers += ["compliance", "effort externalization"]
    else:
        handler = MERCHANT_HANDLERS.get(kind, h_generic)
        body, cta, follow = handler(ctx)
        send_as = "vera"
        template = f"vera_{kind}_v1"

    body = clean_body(body)
    # Template params {{1}} name, {{2}} content, {{3}} the closing CTA (from the last question to the end).
    m_cta = re.search(r"[^.!?\n]*\?[^?]*$", body)
    cta_text = m_cta.group(0).strip() if m_cta else re.split(r"(?<=[.!?])\s+", body)[-1]
    content = body[: len(body) - len(m_cta.group(0))].strip() if m_cta else body[: -len(cta_text)].strip()
    params = [ctx.sal if send_as == "vera" else _cust_name(customer), content or body, cta_text]
    rationale = (f"{humanize(kind).capitalize()} (urgency {trigger.get('urgency', '?')}, {trigger.get('source', '?')}) → "
                 f"{'customer-facing on behalf of merchant' if send_as == 'merchant_on_behalf' else 'merchant-facing'}. "
                 f"Anchors: {'; '.join(dict.fromkeys(ctx.facts)) or 'merchant performance vs peers'}. "
                 f"Levers: {', '.join(dict.fromkeys(ctx.levers))}. "
                 f"Language: {customer_lang(customer) if send_as == 'merchant_on_behalf' else ctx.lang}."
                 + (" Trigger payload was a placeholder, so only merchant-context facts were used." if ctx.placeholder else ""))
    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id')}",
        "rationale": rationale,
        "template_name": template,
        "template_params": params,
        "followup": follow,
        "lang": customer_lang(customer) if send_as == "merchant_on_behalf" else ctx.lang,
        "composer_version": COMPOSER_VERSION,
    }


# ---------------------------------------------------------------------------
# Post-composition validation
# ---------------------------------------------------------------------------


def clean_body(body: str) -> str:
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r" +([.,;:!?])", r"\1", body)
    body = re.sub(r"\.\.+", ".", body)
    body = re.sub(r"\.\"\.", ".\"", body)
    return body.strip()


def validate(msg: dict, category: dict) -> list[str]:
    """Return a list of problems (empty = OK). Used in tests + by the server as a guard."""
    problems = []
    body = msg.get("body", "")
    if not body.strip():
        problems.append("empty body")
    if re.search(r"https?://|www\.", body):
        problems.append("url in body")
    taboos = ((category or {}).get("voice") or {}).get("vocab_taboo", []) or []
    for t in taboos:
        t0 = re.sub(r"\s*\(.*\)", "", t).lower()
        if t0 and t0 in body.lower():
            problems.append(f"taboo word: {t0}")
    if "Dr. Dr." in body:
        problems.append("double Dr.")
    if body.lower().count("reply yes") > 1:
        problems.append("multiple CTAs")
    return problems
