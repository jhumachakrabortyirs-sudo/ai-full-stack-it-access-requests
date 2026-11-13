"""Regenerate a redacted request example without credentials or provider calls.

Run from the repository root: python3 -m tools.request_evidence
"""
import json
from pathlib import Path

from intake.extraction import read_export
from intake.http_provider import HttpProvider, ProviderSettings


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    settings = ProviderSettings(**json.loads((root / "config/provider.json").read_text(encoding="utf-8")))
    source = read_export(root / "materials/requests/IT-001.json")
    evidence = HttpProvider(settings=settings).request_evidence(message=source.message)
    evidence["source"] = {"document_id": source.document_id, "source_sha256": source.source_sha256}
    evidence["configuration_file"] = "config/provider.json"
    destination = root / "deliverables/request-evidence.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("Wrote deliverables/request-evidence.json (request example only; no provider call)")


if __name__ == "__main__":
    main()
