"""Audit saved submission artifacts without a provider call or credential read.

Run from the repository root with the locked environment:
    env -u API_KEY .venv/bin/python tools/validate_submission.py

Writes planning/validation-checks.json only. External submission is not checked.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from intake.accuracy import compare, load_oracle, validate_oracle
from intake.exports import capture
from intake.http_provider import HttpProvider, ProviderSettings


def require(condition, message):
    if not condition:
        raise ValueError(message)


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def main():
    oracle = load_oracle(ROOT / "deliverables/accuracy.md")
    validate_oracle(oracle, root=ROOT)
    snapshot = capture(ROOT / "deliverables")
    queue, records = snapshot.read()  # Verifies both manifest hashes/counts.
    supplied = {path.name for path in (ROOT / "materials/requests").glob("*.json")}
    require(len(supplied) == len(queue) == len(records) == 20, "Expected 20 supplied/current records")
    require({row["document_id"] for row in records} == supplied, "Current extraction IDs differ from supplied exports")
    require({row["document_id"] for row in queue} == supplied, "Queue IDs differ from supplied exports")
    require(len({row["document_id"] for row in records}) == len(records), "Duplicate extraction IDs")
    by_id = {row["document_id"]: row for row in records}
    for row in queue:
        extraction = by_id[row["document_id"]]
        require(all(value == extraction.get(key) for key, value in row.items()), "Queue/extraction fields disagree")
    supplied_oracle = dict(oracle, documents={name: oracle["documents"][name] for name in sorted(supplied)})
    current = compare(records, supplied_oracle, root=ROOT)
    require(current["scalar"] == {"correct": 240, "denominator": 240}, "Current scalar facts differ")
    require(not current["factual_errors"] and current["routing"]["correct"] == 20, "Current evidence or routes differ")
    for name in ("schema", "quotes", "issues"):
        require(current[name]["passed"] == 20, f"Current {name} check failed")
    require(all(row["response_source"] == "mock" for row in records), "Unexpected response provenance")
    for row in records:
        for field, path in (("policy_sha256", "materials/office-policy.md"), ("prompt_sha256", "intake/extraction-prompt.txt"), ("schema_sha256", "snapshot.schema.json")):
            require(row[field] == hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), f"Current {field} differs")

    accuracy = json.loads((ROOT / "deliverables/accuracy-report.json").read_text())
    require(accuracy["scalar"] == {"correct": 252, "denominator": 252}, "Saved accuracy counts differ")
    require(set(accuracy["by_document"]) == set(oracle["documents"]), "Saved accuracy document set differs")
    require(accuracy["oracle_fingerprint"] == hashlib.sha256(json.dumps(oracle, sort_keys=True, ensure_ascii=False).encode()).hexdigest(), "Saved report uses a different oracle")
    require(accuracy["observation_kind"] == "mock-fixture-agreement" and not accuracy["factual_errors"], "Saved accuracy label/errors differ")
    for name in ("schema", "quotes", "issues"):
        require(accuracy[name] == {"passed": 21, "denominator": 21, "failures": []}, f"Saved accuracy {name} differs")
    require(accuracy["routing"] == {"correct": 21, "denominator": 21, "failures": []}, "Saved accuracy routes differ")
    require(len(accuracy["provenance"]) == 21, "Missing saved accuracy provenance")
    for row in accuracy["provenance"]:
        require(row["source_sha256"] == oracle["documents"][row["document_id"]]["source_sha256"], "Saved accuracy source differs")
        require(row["response_source"] == "mock", "Unexpected accuracy provenance")

    evidence = json.loads((ROOT / "deliverables/request-evidence.json").read_text())
    settings = ProviderSettings(**json.loads((ROOT / "config/provider.json").read_text()))
    message = json.loads((ROOT / "materials/requests/IT-001.json").read_text())["message"]
    generated = HttpProvider(settings=settings).request_evidence(message=message)
    for field in ("request", "configuration", "response_handling"):
        require(evidence[field] == generated[field], f"Saved request {field} differs from actual builder")
    require(evidence["observation"]["provider_call_performed"] is False, "Request example mislabelled")

    run = (ROOT / "planning/test-run.txt").read_text()
    # Some failure-path tests emit diagnostic logs before unittest prints ok.
    cases = re.findall(r"^(test_\w+) \([^\n]+\) \.\.\. (.*?)(?=^test_|\n-{10,}|\Z)", run, re.MULTILINE | re.DOTALL)
    passed = [name for name, outcome in cases if outcome.rstrip().endswith("ok")]
    require(len(passed) == 89 and re.search(r"Ran 89 tests in [\d.]+s\s+OK\s*$", run), "A fresh successful full 89-test log is required")
    worksheet = (ROOT / "planning/validation.md").read_text()
    spec_ids = re.findall(r"^\| (R-\d+) \|", (ROOT / "planning/spec.md").read_text(), re.MULTILINE)
    rows = [line.split("|")[1:-1] for line in worksheet.splitlines() if line.startswith("| R-")]
    require([row[0].strip() for row in rows] == spec_ids and len(rows) == 89, "Validation IDs/order differ from spec")
    require(all(row[2].strip() in {"met", "not met"} for row in rows), "Unmarked validation requirements")
    # Validate the worksheet's linked test names against actual passing lines.
    for name, line in re.findall(r"\[(test_\w+)\]\(test-run.txt#L(\d+)\)", worksheet):
        require(name in passed and run.splitlines()[int(line) - 1].startswith(name + " "), f"Broken test evidence link: {name}")
    # Verify each Markdown relative-file link resolves; HTTP links are external.
    for target in re.findall(r"\]\(([^)]+)\)", worksheet):
        if "://" not in target and target != "validation-checks.json":
            require((ROOT / "planning" / target.split("#")[0]).exists(), f"Missing evidence file: {target}")
    counts = Counter(row[2].strip() for row in rows)
    history = git("log", "--reverse", "--format=%s").splitlines()
    steps = [index for index, value in enumerate(history) if re.match(r"Step [1-8]:", value)]
    require(len(steps) == 8, "Expected eight implementation commits")
    prior = -1
    for index in steps:
        require(any(value.startswith("Test first:") for value in history[prior + 1:index]), "Missing preceding test-first commit")
        prior = index

    observation = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "command": "env -u API_KEY .venv/bin/python tools/validate_submission.py",
        "scope": "Saved artifacts, local git evidence and offline tests; no live model or external submission verification",
        "base_commit": git("rev-parse", "HEAD"),
        "local_origin_main_ref": git("rev-parse", "refs/remotes/origin/main"),
        "remote_ref_note": "Local tracking ref only; no network verification or push performed",
        "generation": snapshot.generation,
        "current_records": len(records),
        "current_statuses": dict(Counter(row["status"] for row in records)),
        "current_scalar": current["scalar"],
        "current_schema_quotes_issues_routes": "20/20 each; original byte hashes and all queue fields agree",
        "saved_accuracy_scalar": accuracy["scalar"],
        "saved_accuracy_kind": accuracy["observation_kind"],
        "request_evidence": "Matches configured request builder; no credential read or provider call",
        "full_suite": {"passed": len(passed), "log_sha256": hashlib.sha256(run.encode()).hexdigest()},
        "test_first_implementation_pairs": len(steps),
        "validation": dict(counts),
        "external_requirements_not_verified": ["R-78", "R-79", "R-80"],
    }
    target = ROOT / "planning/validation-checks.json"
    target.write_text(json.dumps(observation, indent=2) + "\n")
    print(json.dumps(observation, indent=2))


if __name__ == "__main__":
    main()
