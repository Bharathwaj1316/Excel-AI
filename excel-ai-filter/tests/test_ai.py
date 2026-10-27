"""End-to-end check of the offline AI (needs Ollama running with the model).
Run:  .venv\\Scripts\\python.exe tests\\test_ai.py"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ai  # noqa: E402
from filters import apply_spec, load_table, resolve_spec  # noqa: E402

df = load_table((ROOT / "samples" / "sample_data.xlsx").read_bytes(), "sample_data.xlsx", "Sheet1", 0)

CASES = {
    "chennai customers": ["Arun", "Karthik"],
    "sales above 10k": ["Arun", "Karthik", "Suresh"],
    "top 2 highest sales": ["Karthik", "Suresh"],
    "customers from madurai or trichy": ["Priya", "Suresh"],
    "chenai la irukura customers": ["Arun", "Karthik"],
}

ready, message = ai.status()
if not ready:
    sys.exit(message)

failed = 0
for question, expected in CASES.items():
    start = time.time()
    spec, _ = resolve_spec(ai.question_to_spec(question, df), df)
    got = sorted(apply_spec(df, spec)["Customer Name"])
    ok = got == sorted(expected)
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} [{time.time() - start:.0f}s] {question!r} -> {got}")

print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
sys.exit(1 if failed else 0)
