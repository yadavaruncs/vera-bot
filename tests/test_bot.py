"""End-to-end tests against the FastAPI app (run: python -m pytest -q tests)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import bot  # noqa: E402
from composer import compose, validate  # noqa: E402
from dataset_loader import load_all  # noqa: E402

DATA = load_all()
QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about"]


@pytest.fixture()
def client():
    bot.STORE.reset()
    c = TestClient(bot.app)
    for slug, cat in DATA["categories"].items():
        assert c.post("/v1/context", json={"scope": "category", "context_id": slug, "version": 1, "payload": cat,
                                           "delivered_at": "2026-04-26T10:00:00Z"}).status_code == 200
    for mid, m in DATA["merchants"].items():
        c.post("/v1/context", json={"scope": "merchant", "context_id": mid, "version": 1, "payload": m, "delivered_at": "x"})
    for cid, cu in DATA["customers"].items():
        c.post("/v1/context", json={"scope": "customer", "context_id": cid, "version": 1, "payload": cu, "delivered_at": "x"})
    return c


def push_trigger(c, tid):
    c.post("/v1/context", json={"scope": "trigger", "context_id": tid, "version": 1,
                                "payload": DATA["triggers"][tid], "delivered_at": "x"})


def test_warmup_counts_and_idempotency(client):
    h = client.get("/v1/healthz").json()
    assert h["contexts_loaded"] == {"category": 5, "merchant": 50, "customer": 200, "trigger": 0}
    m = DATA["merchants"]["m_001_drmeera_dentist_delhi"]
    r = client.post("/v1/context", json={"scope": "merchant", "context_id": m["merchant_id"], "version": 1, "payload": m, "delivered_at": "x"})
    assert r.status_code == 409 and r.json()["current_version"] == 1
    r = client.post("/v1/context", json={"scope": "bogus", "context_id": "x", "version": 1, "payload": {}, "delivered_at": "x"})
    assert r.status_code == 400
    assert client.get("/v1/metadata").status_code == 200


def test_every_trigger_composes_cleanly():
    for tid, t in DATA["triggers"].items():
        m = DATA["merchants"][t["merchant_id"]]
        cu = DATA["customers"].get(t.get("customer_id")) if t.get("customer_id") else None
        cat = DATA["categories"][m["category_slug"]]
        out = compose(cat, m, t, cu)
        assert not validate(out, cat), (tid, validate(out, cat), out["body"])
        for k in ("body", "cta", "send_as", "suppression_key", "rationale"):
            assert out[k], (tid, k)
        assert "{" not in out["body"] and "None" not in out["body"], (tid, out["body"])
        assert "â" not in out["body"], (tid, "mojibake")


def test_tick_all_triggers_and_suppression(client):
    tids = list(DATA["triggers"])
    for t in tids:
        push_trigger(client, t)
    sent = []
    for i in range(0, len(tids), 5):
        r = client.post("/v1/tick", json={"now": "2026-04-26T10:30:00Z", "available_triggers": tids[i:i + 5]})
        assert r.status_code == 200
        sent += r.json()["actions"]
    assert len(sent) >= 60
    for a in sent:
        for k in ("conversation_id", "merchant_id", "send_as", "trigger_id", "cta", "suppression_key", "rationale", "body"):
            assert a.get(k) is not None, k
    # re-ticking the same triggers must not resend (suppression)
    again = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": tids[:5]}).json()["actions"]
    assert all(a["trigger_id"] not in {s["trigger_id"] for s in sent} for a in again)


def _start(client, tid):
    push_trigger(client, tid)
    acts = client.post("/v1/tick", json={"now": "2026-04-26T10:30:00Z", "available_triggers": [tid]}).json()["actions"]
    assert acts
    return acts[0]


def _reply(client, conv, mid, msg, turn, role="merchant", cid=None):
    return client.post("/v1/reply", json={"conversation_id": conv, "merchant_id": mid, "customer_id": cid,
                                          "from_role": role, "message": msg, "received_at": "x", "turn_number": turn}).json()


def test_auto_reply_hell_same_conversation(client):
    a = _start(client, "trg_022_cde_webinar_dentists")
    auto = "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."
    r1 = _reply(client, a["conversation_id"], a["merchant_id"], auto, 2)
    r2 = _reply(client, a["conversation_id"], a["merchant_id"], auto, 3)
    r3 = _reply(client, a["conversation_id"], a["merchant_id"], auto, 4)
    assert r1["action"] == "send" and "auto-reply" in r1["body"].lower()
    assert r2["action"] == "wait"
    assert r3["action"] == "end"


def test_auto_reply_across_conversations_like_simulator(client):
    mid = "m_002_bharat_dentist_mumbai"
    auto = "Thank you for contacting us! Our team will respond shortly."
    actions = [_reply(client, f"conv_auto_{i}", mid, auto, i + 1)["action"] for i in range(1, 5)]
    assert "end" in actions[:3]


def test_intent_transition_goes_to_action(client):
    a = _start(client, "trg_001_research_digest_dentists")
    _reply(client, a["conversation_id"], a["merchant_id"], "Interesting. Is this relevant for my patients?", 2)
    r = _reply(client, a["conversation_id"], a["merchant_id"], "Ok lets do it. Whats next?", 3)
    assert r["action"] == "send"
    low = r["body"].lower()
    assert not any(q in low for q in QUALIFYING), r["body"]
    assert any(w in low for w in ["draft", "confirm", "here"]), r["body"]
    r2 = _reply(client, a["conversation_id"], a["merchant_id"], "Confirm", 4)
    assert r2["action"] == "send" and "live" in r2["body"].lower()


def test_join_intent_hindi(client):
    r = _reply(client, "conv_join", "m_010_sunrisepharm_pharmacy_lucknow", "Mujhe magicpin judrna hai", 2)
    assert r["action"] == "send" and "confirm" in r["body"].lower()
    assert not any(q in r["body"].lower() for q in QUALIFYING)


def test_hostile_then_off_topic_then_stop(client):
    a = _start(client, "trg_023_competitor_opened_dentist")
    conv, mid = a["conversation_id"], a["merchant_id"]
    r1 = _reply(client, conv, mid, "You people are useless, why do you keep bothering", 2)
    assert r1["action"] == "send" and "stop" in r1["body"].lower()
    r2 = _reply(client, conv, mid, "can you also help me file my GST?", 3)
    assert r2["action"] in ("send", "end")
    if r2["action"] == "send":
        assert "gst" in r2["body"].lower()
    r3 = _reply(client, conv, mid, "Stop messaging me.", 4)
    assert r3["action"] == "end"
    # merchant is now suppressed for proactive sends
    push_trigger(client, "trg_001_research_digest_dentists")
    acts = client.post("/v1/tick", json={"now": "2026-04-26T11:00:00Z", "available_triggers": ["trg_001_research_digest_dentists"]}).json()["actions"]
    assert acts == []


def test_off_topic_redirect(client):
    a = _start(client, "trg_018_supply_atorvastatin_recall")
    r = _reply(client, a["conversation_id"], a["merchant_id"], "Btw can you also help me with my GST filing this month?", 2)
    assert r["action"] == "send" and "gst" in r["body"].lower() and "reply yes" in r["body"].lower()


def test_defer_waits(client):
    a = _start(client, "trg_012_milestone_mylari")
    r = _reply(client, a["conversation_id"], a["merchant_id"], "busy right now, message me tomorrow", 2)
    assert r["action"] == "wait" and r["wait_seconds"] == 86400


def test_customer_slot_pick(client):
    a = _start(client, "trg_003_recall_due_priya")
    assert a["send_as"] == "merchant_on_behalf"
    r = _reply(client, a["conversation_id"], a["merchant_id"], "2", 2, role="customer", cid=a["customer_id"])
    assert r["action"] == "send" and "Thu 6 Nov" in r["body"]


def test_no_repeat_bodies(client):
    a = _start(client, "trg_011_review_theme_late_delivery")
    bodies = [a["body"]]
    for i, msg in enumerate(["hmm", "hmm ok", "not sure", "what else"]):
        r = _reply(client, a["conversation_id"], a["merchant_id"], msg, i + 2)
        if r["action"] == "send":
            assert r["body"] not in bodies
            bodies.append(r["body"])


def test_context_update_is_used(client):
    m = dict(DATA["merchants"]["m_002_bharat_dentist_mumbai"])
    m["performance"] = dict(m["performance"], views=1234)
    client.post("/v1/context", json={"scope": "merchant", "context_id": m["merchant_id"], "version": 2, "payload": m, "delivered_at": "x"})
    a = _start(client, "trg_005_renewal_due_bharat")
    assert "1,234 views" in a["body"]


def test_no_consent_customer_not_messaged():
    t = {"id": "trg_x", "scope": "customer", "kind": "customer_lapsed_soft", "merchant_id": "m_010_sunrisepharm_pharmacy_lucknow",
         "customer_id": "c_015_anonymous_for_m010", "payload": {}, "urgency": 2, "suppression_key": "x"}
    m = DATA["merchants"]["m_010_sunrisepharm_pharmacy_lucknow"]
    out = compose(DATA["categories"]["pharmacies"], m, t, DATA["customers"]["c_015_anonymous_for_m010"])
    assert out["send_as"] == "vera" and "opt-in" in out["body"]
