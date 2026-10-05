"""Deterministic synthetic transfers ONLY for engineering tests, never research."""
from __future__ import annotations

import math
import random
from datetime import timedelta
from pathlib import Path

from .analyze import snapshot_from_events
from .common import TOKENS, iso, utc


def make_fixture(output: Path) -> dict:
    start, end = '2026-04-01T00:00:00Z', '2026-10-01T00:00:00Z'
    begin, finish = utc(start), utc(end)
    rng = random.Random(42)
    categories = ('dex', 'lending', 'bridge')
    addresses = {category: '0x' + f'{i+1:040x}' for i, category in enumerate(categories)}
    labels = [{"address": address, "entity": f"Synthetic {category}", "category": category, "verified": True,
               "source_url": "synthetic-test://fixture", "retrieved_at": start, "valid_from": None, "valid_to": None,
               "evidence": "Invented test address, not a real entity label"} for category, address in addresses.items()]
    events = []
    number = 0
    for hour in range(int((finish-begin).total_seconds()//3600)):
        when = begin + timedelta(hours=hour)
        for token_i, (token, info) in enumerate(TOKENS.items()):
            for cat_i, category in enumerate(categories):
                seasonal = 1.0 + 0.3*math.sin(hour*2*math.pi/24) + 0.15*math.cos(hour*2*math.pi/168)
                for direction in (1, -1):
                    base = (1000 + cat_i*800 + token_i*200) * seasonal
                    amount = base * rng.lognormvariate(0, 0.12) * (1.0 if direction == 1 else 0.82)
                    if hour in (3500, 4100, 4200) and cat_i == 0 and token_i == 0 and direction == 1:
                        amount *= 15
                    user = '0x' + f'{100+rng.randrange(30):040x}'
                    source, target = (user, addresses[category]) if direction == 1 else (addresses[category], user)
                    events.append({"chain_id": 1, "block_timestamp": iso(when), "token_address": info['address'],
                                   "from_address": source, "to_address": target, "amount_raw": str(round(amount*1e6)),
                                   "tx_hash": '0x'+f'{number+1:064x}', "log_index": 0})
                    number += 1
    return snapshot_from_events(events, labels, start, end, output, kind='synthetic-test',
                                complete_months=[f'2026-{m:02}' for m in range(4,10)],
                                source={"type":"synthetic engineering fixture", "seed":42, "research_evidence":False})
