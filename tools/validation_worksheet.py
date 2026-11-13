"""Prepare evidence links for human spec review; never assign validation marks.

Run after saving a passing planning/test-run.txt. An existing worksheet is
preserved so human assessment cannot be overwritten by regeneration.
"""
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "1": [("intake/providers.py", "class ScriptedProvider")],
    "2": [("intake/extraction.py", "def validate_response")],
    "3": [("intake/review.py", "def route_extraction"), ("intake/extraction-prompt.txt", None)],
    "4": [("intake/http_provider.py", "class HttpProvider"), ("intake/processing.py", "def process_file")],
    "5": [("intake/state.py", "class StateStore")],
    "6": [("intake/workers.py", "def run_intake")],
    "7": [("intake/cli.py", "def main"), ("intake/exports.py", "def publish")],
    "8": [("intake/accuracy.py", "def compare"), ("deliverables/accuracy.md", None), ("deliverables/verification.md", None)],
}


def _rows(text):
    result = {}
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        parts = [part.strip() for part in re.split(r"(?<!\\)\|", line)]
        if len(parts) >= 4 and re.fullmatch(r"R-\d{2}", parts[1]):
            result[parts[1]] = parts[2:-1]
    return result


def _test_ids(text):
    identifiers = []
    for match in re.finditer(r"T(\d{2})(?:[–-]T?(\d{2}))?", text):
        first = int(match.group(1))
        last = int(match.group(2)) if match.group(2) else first
        identifiers.extend(f"T{number:02}" for number in range(first, last + 1))
    return list(dict.fromkeys(identifiers))


def main() -> None:
    target = ROOT / "planning/validation.md"
    if target.exists():
        print("Preserved existing planning/validation.md; human marks are never overwritten")
        return
    run = (ROOT / "planning/test-run.txt").read_text(encoding="utf-8")
    if not re.search(r"^OK\s*$", run, flags=re.MULTILINE) or re.search(r"^FAILED", run, flags=re.MULTILINE):
        raise ValueError("Save a complete passing full test run before preparing this worksheet")
    spec = _rows((ROOT / "planning/spec.md").read_text(encoding="utf-8"))
    plan = (ROOT / "planning/plan.md").read_text(encoding="utf-8")
    coverage = _rows(plan.split("# Coverage", 1)[1].split("## Not yet covered", 1)[0])
    external_ids = {"R-78", "R-79", "R-80"}
    if set(coverage) - set(spec) or (set(spec) - set(coverage)) - external_ids:
        raise ValueError("Spec IDs and planned coverage IDs do not agree")
    for identifier in external_ids:
        coverage.setdefault(identifier, ["external", "External human/facilitator evidence required"])
    tests = {}
    for line_number, line in enumerate(run.splitlines(), 1):
        match = re.match(r"^(test_(T\d{2})_\w+).*? \.\.\. ", line)
        if match:
            tests.setdefault(match.group(2), []).append((match.group(1), line_number))
    text = """# Spec validation worksheet — human marks pending

This worksheet has one row per requirement ID in planning/spec.md. The source
spec contains table headers as well as requirements; only requirement rows are
counted here. No met/not-met decisions have been made by the agent.

The human reviewer must open the evidence, inspect each test's assertions and
the relevant code, then replace each dash with **met** or **not met** and update
the counts below. Partial or absent evidence is not met under the instructions
in [docs/03-implement-and-validate.md](../docs/03-implement-and-validate.md).

Evidence links are candidate review aids derived from the committed plan and
the actual full run, not an assertion that every clause is satisfied. Up to
four test links are shown per row; additional cases remain in the plan's
coverage table. Mock checks do not establish live model accuracy, availability
or throughput. R-78–R-80 require external human/facilitator verification.

| Spec line | Requirement | Met or not met | Evidence | Note |
|---|---|---|---|---|
"""
    external = {
        "R-78": "Human/facilitator must verify the session capture paired with the submitted commit; not certified here.",
        "R-79": "Human must commit/push and inspect the default branch submission; no push performed here.",
        "R-80": "Human must submit the root repository URL through Classroom; no submission performed here.",
    }
    for identifier, values in spec.items():
        requirement = values[0]
        step, cases = coverage[identifier][:2]
        candidates = []
        for case in _test_ids(cases):
            candidates.extend(tests.get(case, []))
        links = [f"[{name}](test-run.txt#L{number})" for name, number in candidates[:4]]
        source_paths = []
        for match in re.finditer(r"(\d+)(?:[–-](\d+))?", step):
            first = int(match.group(1))
            last = int(match.group(2)) if match.group(2) else first
            for number in range(first, last + 1):
                source_paths.extend(SOURCES.get(str(number), []))
        for relative, marker in dict.fromkeys(source_paths):
            lines = (ROOT / relative).read_text(encoding="utf-8").splitlines()
            number = next((index for index, line in enumerate(lines, 1) if marker is not None and line.startswith(marker)), 1)
            links.append(f"[{relative}:L{number}](../{relative}#L{number})")
        note = external.get(identifier, "Pending human review; confirm the evidence covers every clause.")
        if not links:
            links = ["External evidence required"]
        text += f"| {identifier} | {requirement} | — | {'<br>'.join(links)} | {note} |\n"
    text += f"\nMarked met: 0. Marked not met: 0. Pending human review: {len(spec)}.\n"
    target.write_text(text, encoding="utf-8")
    print(f"Created planning/validation.md with {len(spec)} requirement rows and candidate evidence; human marks remain pending")


if __name__ == "__main__":
    main()
