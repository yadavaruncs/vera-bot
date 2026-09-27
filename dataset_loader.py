"""Load the challenge dataset (seed files expanded deterministically) into dicts."""

from __future__ import annotations

import json
import random
from pathlib import Path

ROOT = Path(__file__).parent
DATASET = ROOT / "dataset"


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_seeds_utf8(seed_dir: Path):
    """Same as generate_dataset.load_seeds but UTF-8 safe (the original mangles ₹ on Windows)."""
    categories = {}
    for f in sorted((seed_dir / "categories").glob("*.json")):
        data = _read(f)
        categories[data["slug"]] = data
    return (categories,
            _read(seed_dir / "merchants_seed.json")["merchants"],
            _read(seed_dir / "customers_seed.json")["customers"],
            _read(seed_dir / "triggers_seed.json")["triggers"])


def load_all() -> dict:
    """Return {"categories", "merchants", "customers", "triggers", "test_pairs"} using the official generator."""
    import sys
    sys.path.insert(0, str(DATASET))
    import generate_dataset as gen  # noqa: E402

    rnd = random.Random(gen.SEED)
    categories, m_seeds, c_seeds, t_seeds = load_seeds_utf8(DATASET)
    merchants = gen.expand_merchants(m_seeds, rnd)
    customers = gen.expand_customers(c_seeds, merchants, rnd)
    triggers = gen.expand_triggers(t_seeds, merchants, customers, rnd)

    pairs_file = DATASET / "expanded" / "test_pairs.json"
    if pairs_file.exists():
        pairs = json.loads(pairs_file.read_text(encoding="utf-8"))["pairs"]
    else:
        by_kind: dict[str, list] = {}
        for t in triggers:
            by_kind.setdefault(t["kind"], []).append(t)
        pairs = []
        for kind, ts in sorted(by_kind.items()):
            for t in ts[:2]:
                pairs.append({"test_id": f"T{len(pairs) + 1:02d}", "trigger_id": t["id"],
                              "merchant_id": t["merchant_id"], "customer_id": t.get("customer_id")})
        pairs = pairs[:30]

    return {
        "categories": categories,
        "merchants": {m["merchant_id"]: m for m in merchants},
        "customers": {c["customer_id"]: c for c in customers},
        "triggers": {t["id"]: t for t in triggers},
        "test_pairs": pairs,
    }
