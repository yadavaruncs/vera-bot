"""Print sample multi-turn transcripts (python tests/replay_demo.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import bot  # noqa: E402
from dataset_loader import load_all  # noqa: E402

DATA = load_all()
SCENARIOS = [
    ("Engaged → commit → confirm", "trg_001_research_digest_dentists",
     ["Interesting. How would this work for my clinic?", "Ok let's do it, what's next?", "Confirm", "Thanks!"]),
    ("Auto-reply hell", "trg_022_cde_webinar_dentists",
     ["Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."] * 4),
    ("Hostile → off-topic", "trg_004_perf_dip_bharat",
     ["Why are you people always bothering me, this is useless", "Can you help me file my GST instead?", "ok fine, what's the offer thing"]),
    ("Hindi merchant", "trg_013_corporate_thali_planning",
     ["Haan theek hai, kar do", "Kitna time lagega live hone mein?", "confirm"]),
    ("Customer slot pick", "trg_003_recall_due_priya", ["1", "thanks"]),
]


def main():
    c = TestClient(bot.app)
    for scope, key in (("category", "categories"), ("merchant", "merchants"), ("customer", "customers"), ("trigger", "triggers")):
        for cid, payload in DATA[key].items():
            c.post("/v1/context", json={"scope": scope, "context_id": cid, "version": 1, "payload": payload, "delivered_at": "x"})
    for title, tid, replies in SCENARIOS:
        print(f"\n=== {title} ({tid})")
        act = c.post("/v1/tick", json={"now": "2026-04-26T10:30:00Z", "available_triggers": [tid]}).json()["actions"][0]
        print(f"[VERA]  {act['body']}")
        role = "customer" if act["send_as"] == "merchant_on_behalf" else "merchant"
        for i, msg in enumerate(replies):
            print(f"[{role.upper()[:4]}]  {msg}")
            r = c.post("/v1/reply", json={"conversation_id": act["conversation_id"], "merchant_id": act["merchant_id"],
                                         "customer_id": act["customer_id"], "from_role": role, "message": msg,
                                         "received_at": "x", "turn_number": i + 2}).json()
            print(f"[VERA]  <{r['action']}> {r.get('body', '')}  ({r.get('rationale', '')})")
            if r["action"] == "end":
                break


if __name__ == "__main__":
    main()
