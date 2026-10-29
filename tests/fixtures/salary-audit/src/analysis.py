"""Within-pair gender gap in log starting salary (pair fixed effects)."""
import json
import math
import statistics
import sys

sys.path.insert(0, "src")
from parse_salary import parse_salary  # noqa: E402

rows = [json.loads(l) for l in open("data/responses.jsonl")]
for r in rows:
    r["salary"] = parse_salary(r["response"])

pairs = {}
for r in rows:
    if r["salary"] is not None:
        pairs.setdefault(r["pair_id"], {})[r["gender"]] = r["salary"]

diffs = [math.log(p["female"]) - math.log(p["male"]) for p in pairs.values()
         if "female" in p and "male" in p]
n = len(diffs)
mean = statistics.mean(diffs)
se = statistics.stdev(diffs) / math.sqrt(n)
t = mean / se

with open("out/results.txt", "w") as f:
    f.write("Paired-resume audit: within-pair log salary gap (female - male)\n")
    f.write(f"responses: {len(rows)}\n")
    f.write(f"complete pairs: {n}\n")
    f.write(f"mean log gap: {mean:.4f}\n")
    f.write(f"se: {se:.4f}\n")
    f.write(f"t: {t:.2f}\n")
    f.write(f"median log gap: {statistics.median(diffs):.4f}\n")
