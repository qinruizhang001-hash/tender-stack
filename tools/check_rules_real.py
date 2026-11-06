import csv
import sys

sys.path.insert(0, "adapter")
from verify_rules import apply_rules

rows = list(csv.DictReader(open("out/armC_real/ledger/requirements.csv", encoding="utf-8-sig")))
print(f"台账 {len(rows)} 条")

# 1) 空证据不应产生任何命中
hits_empty = sum(1 for r in rows if apply_rules(r["requirement"], "", "已响应")[1])
print(f"证据为空时命中规则：{hits_empty} 条（应为 0）")

# 2) 证据=要求原文：数值必然相等，不应出现负偏离/未响应
bad = []
for r in rows:
    status, hits = apply_rules(r["requirement"], r["requirement"], "已响应")
    if status in ("负偏离", "未响应"):
        bad.append((r["req_id"], status, r["requirement"][:40], hits[0].detail if hits else ""))
print(f"自比对误判（负偏离/未响应）：{len(bad)} 条")
for item in bad[:10]:
    print("   ", item)

# 3) 命中分布
confirm = sum(1 for r in rows if any(h.severity == "confirm" for h in apply_rules(r["requirement"], r["requirement"], "已响应")[1]))
print(f"需要人工确认（含数值/资质）：{confirm} 条 / {len(rows)}")
