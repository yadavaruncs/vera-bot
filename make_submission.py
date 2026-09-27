"""Generate submission.jsonl for the 30 canonical test pairs (python make_submission.py)."""

import json
import time
from pathlib import Path

from bot import compose
from composer import validate
from dataset_loader import load_all

OUT = Path(__file__).parent / "submission.jsonl"


def main():
    d = load_all()
    lines, worst = [], 0.0
    for pair in d["test_pairs"]:
        trigger = d["triggers"][pair["trigger_id"]]
        merchant = d["merchants"][pair["merchant_id"]]
        customer = d["customers"].get(pair["customer_id"]) if pair.get("customer_id") else None
        category = d["categories"][merchant["category_slug"]]
        t0 = time.perf_counter()
        msg = compose(category, merchant, trigger, customer)
        worst = max(worst, time.perf_counter() - t0)
        problems = validate(msg, category)
        assert not problems, (pair["test_id"], problems)
        assert msg == compose(category, merchant, trigger, customer), "compose must be deterministic"
        lines.append({"test_id": pair["test_id"], "trigger_id": pair["trigger_id"], "merchant_id": pair["merchant_id"],
                      "customer_id": pair.get("customer_id"), "body": msg["body"], "cta": msg["cta"],
                      "send_as": msg["send_as"], "suppression_key": msg["suppression_key"], "rationale": msg["rationale"],
                      "template_name": msg["template_name"], "template_params": msg["template_params"]})
    OUT.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n", encoding="utf-8")
    print(f"wrote {len(lines)} lines to {OUT.name}; slowest compose {worst * 1000:.1f} ms")


if __name__ == "__main__":
    main()
