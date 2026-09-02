import json
from pathlib import Path

lines = Path("submission.jsonl").read_text(encoding="utf-8").strip().split("\n")
print(f"Total lines: {len(lines)}\n")

for i, line in enumerate(lines):
    r = json.loads(line)
    words = len(r["body"].split())
    flag = " *** OVER 70 ***" if words > 70 else ""
    print(f"[{r['test_id']}] {r['trigger_kind']:30s} {words:3d}w  {r['cta']:25s}  {r['send_as']}{flag}")

word_counts = [len(json.loads(l)["body"].split()) for l in lines]
print(f"\nAvg words: {sum(word_counts)/len(word_counts):.1f}  Max: {max(word_counts)}  Min: {min(word_counts)}")
over = sum(1 for w in word_counts if w > 70)
print(f"Over 70 words: {over}/{len(lines)}")
