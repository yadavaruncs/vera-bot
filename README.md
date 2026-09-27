# Vera, rebuilt: a grounded merchant-engagement bot

**Live demo (runs the real bot code in your browser):** https://yadavaruncs.github.io/vera-bot/

**Run:** `pip install -r requirements.txt && uvicorn bot:app --host 0.0.0.0 --port 8080`
**Regenerate submission:** `python make_submission.py` · **Tests:** `python -m pytest -q tests` · **Sample conversations:** `python tests/replay_demo.py`

## Approach
- **One composer, dispatched by `trigger.kind`** ([composer.py](composer.py)). There are 26 kind handlers: 18 merchant-facing and 8 customer-facing. Each one reads all four contexts and builds the message from facts it finds there. Unknown kinds (weather, local news and so on) go to a generic handler that anchors on the trigger payload plus the merchant's clearest gap against peers.
- **Nothing is invented.** Every number, name, date, price and citation in a message comes from the contexts, or is simple arithmetic on them (for example ₹299 − ₹199 = ₹100, or 245 members × 10% churn ≈ 24 per month). Where the case studies invented details ("complimentary fluoride", named office buildings, a ₹2,499 skin-prep price), this bot leaves them out.
- **Judgment, not just templates:**
  - The Saturday IPL match gets a delivery-only plan, because the digest shows Saturday matches cut covers by 12%. The message also notes that the Tue–Thu BOGO doesn't apply and that there are 4 late-delivery reviews.
  - Diwali 188 days out becomes a planning message, not a discount.
  - The bride 196 days out is told the skin-prep can wait, but the Oct–Dec calendar fills up.
  - The refill reminder for atorvastatin mentions the active batch recall.
  - The competitor message says not to race on price, and leans on the merchant's own review quote plus the 78 lapsed patients.
- **Category voice and language:** salutations follow the category ("Dr. Meera", and never "Dr. Dr."). Vocabulary is category-specific and banned words are validated. Merchants in Hindi-belt cities who list `hi` get light Hinglish. Customers get the language they prefer (hi / hi-en / en, plus Vanakkam or Namaskara for ta/kn). Customer messages are sent `merchant_on_behalf`. If a customer has **no consent**, they are never messaged; the bot tells the merchant instead.
- **Placeholder payloads:** 75 of the 100 triggers carry `{"placeholder": true}`. For these, the bot falls back to verifiable merchant facts: 7-day deltas, gaps against peers, review themes and active offers. It never makes up the missing event details, and the rationale says so.
- **Multi-turn** ([conversation_handlers.py](conversation_handlers.py)) is an explicit policy ladder:
  - An opt-out ends the conversation and suppresses the merchant for 30 days.
  - An auto-reply (known pattern, or the same text repeated *across conversations*) gets one nudge to the owner, then a 24h wait, then an end.
  - Hostility gets an apology and a way to opt out.
  - A **commitment switches straight to action mode**: the bot delivers the draft or artifact and asks for CONFIRM, with no further qualifying questions. "Mujhe magicpin judrna hai" starts onboarding.
  - Off-topic asks (GST and similar) are declined politely and the bot returns to the thread's single CTA.
  - "Busy" or "later" leads to a wait.
  - Language is detected on every turn, the same text is never sent twice, and there are at most 5 bot turns per conversation.
- **Server** ([bot.py](bot.py)):
  - `/v1/context` stores versioned context: an older or equal version returns 409, and a bad scope returns 400.
  - Replies always use the *latest* context version, so injected digest or performance updates show up in the next message.
  - `/v1/tick` sorts triggers by urgency, dedupes by suppression key and by identical body per recipient, caps at 2 messages per merchant per tick, and skips customer triggers until the customer context has arrived.

## Tradeoffs
- **Deterministic rules over free-form LLM generation.** This gives zero hallucination, is reproducible, and runs in under 1 ms per compose, so there is no risk of hitting the 30s timeout. The cost is less stylistic variety. An optional LLM ([llm.py](llm.py), `ANTHROPIC_API_KEY`, temperature 0, 8s timeout) is used **only** for off-script merchant questions. Its answer is rejected if it contains a URL or a number that isn't in the context.
- **Trusting the judge's `available_triggers` over `expires_at`.** The simulator's clock is real time, while the trigger dates are simulated.
- **Restraint:** the bot holds a message rather than sending a generic one (no consent, missing customer context, or a duplicate body).

## Context that would have helped most
1. **Real appointment/slot data and service history** for generated customers. `appointment_tomorrow` has no time and `services_received` is empty, which caps how specific customer messages can be.
2. **Payloads for the generated triggers.** Competitor name and offer, festival name and date, and milestone value are all placeholders.
3. **Merchant-level price list and capacity** (open slots, menu prices). This would allow service+price offers without borrowing from the category catalogue.
4. **Vera's own pricing and renewal terms**, so the bot can answer "how much?" instead of deferring.
