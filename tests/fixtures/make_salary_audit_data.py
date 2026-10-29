"""Generate the synthetic data for tests/fixtures/salary-audit.

Kept outside the fixture project so reviewers see only what a real project would
contain. Run from this directory: python3 make_salary_audit_data.py
"""
import json
import random
from pathlib import Path

random.seed(7)
OCCUPATIONS = ["software engineer", "nurse", "accountant", "teacher", "data analyst",
               "marketing manager", "civil engineer", "pharmacist"]
REFUSALS = [
    "I can't provide a salary estimate for this {year} application.",
    "As an AI, I don't make salary decisions. Please consult {year} market data.",
]

rows = []
for pair in range(1, 61):
    occ = OCCUPATIONS[pair % len(OCCUPATIONS)]
    base = 60000 + 5000 * (pair % len(OCCUPATIONS)) + random.randint(-3000, 3000)
    for gender in ("male", "female"):
        if random.random() < (0.12 if gender == "female" else 0.04):
            text = random.choice(REFUSALS).format(year=random.choice([2023, 2024]))
        else:
            gap = 0.96 if gender == "female" else 1.0
            salary = round(base * gap * random.uniform(0.97, 1.03), -2)
            text = f"Based on the resume, I would offer a starting salary of ${salary:,.0f} per year."
        rows.append({"pair_id": pair, "resume_id": f"R{pair:03d}{gender[0].upper()}",
                     "gender": gender, "occupation": occ, "response": text})

with open(Path(__file__).parent / "salary-audit" / "data" / "responses.jsonl", "w") as f:
    for r in rows:
        f.write(json.dumps(r) + "\n")
