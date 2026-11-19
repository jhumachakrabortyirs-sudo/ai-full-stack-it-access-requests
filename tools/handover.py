"""Build handover artifacts from independent expectations and observed mocks.

Run from the repository root: env -u API_KEY python3 -m tools.handover
Expected facts are read only from the authored oracle. Measured records feed
the observation/report sections; they never generate their own expectations.
"""
import json
from pathlib import Path

from intake.accuracy import compare, validate_oracle
from intake.mock_catalog import CatalogProvider
from intake.state import ProcessingConfig
from intake.workers import run_intake
from tools.request_evidence import main as request_evidence


ROOT = Path(__file__).resolve().parents[1]


def _json(value) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)


def _accuracy_document(oracle, report) -> str:
    summary = {
        "observation_kind": report["observation_kind"], "scalar": report["scalar"],
        "documents": len(oracle["documents"]),
        **{key: {field: report[key][field] for field in ("passed", "denominator")} for key in ("schema", "quotes", "issues")},
        "routing": {key: report["routing"][key] for key in ("correct", "denominator")},
        "factual_errors": len(report["factual_errors"]),
    }
    text = """# Source-derived accuracy comparison

<!-- accuracy-oracle: ../tests/accuracy_expected.json -->
[Source-derived oracle](../tests/accuracy_expected.json) is the machine-readable
input to this procedure. Its scalar facts come from the supplied messages and
the source ledger in `planning/plan.md`, authored before the provider fixtures.
The extra quote and issue requirements were written before the deliberately
wrong comparison fixture. Measurements never generate expected values.

This ledger covers all 20 supplied messages and the authored SYN-001 paraphrase.
Each section below includes all 12 expected scalar fields, the original source
hash, literal source evidence and the independently expected routing outcome.

## Procedure and denominator

Compare each of the 12 scalar schema fields by exact-match value and type.
Strings are case-sensitive; there is no trimming, semantic similarity scoring,
date inference or post-comparison normalization. Enums and explicit dates are
already normalized according to policy in the expected facts. Business-purpose
expectations use quoted source wording. A present null matches expected null;
a missing key never counts as a correct null.

The denominator is always 21 × 12 = 252 for this oracle. An absent record,
failed extraction, null extraction or malformed/non-object extraction earns
zero of its 12 scalar matches, rather than dropping that document. A dictionary
with a missing key loses that field's match; remaining exact scalar facts are
still compared. Schema validity is checked independently against all 14
properties. Duplicate documents, unexpected document IDs, stale oracle source
hashes and mismatched measured source hashes are rejected before reporting.

Report schema validity, supporting_quotes coverage/substrings, issues coverage
and routing separately. Each observed quote must be a literal source substring;
required source fragments must be covered by at least one observed quote.
This checks evidence retention, including both conflicting managers; it does
not prove every semantic relationship in arbitrary prose. Issue coverage uses
the authored field/keyword patterns, rather than exact prose matching. Routing
compares status, owner and next_action as one separate per-document result.

Factual-error entries identify the document, field, expected value, actual value,
whether the key was present, and failure reason. Lost source evidence and missing
issues also get explicit entries. Per-field and per-document counts reconcile
with the total. Record source hashes, response_source, model/configuration,
prompt/schema hashes, policy version/hash and attempt provenance with the report.

## Observed mock fixture agreement

The observation below uses the explicit mock catalog and scripted fixtures.
It is not a measurement of live model accuracy. No live provider was called.
Mock agreement checks comparison plumbing and application behavior. Live
semantic extraction quality, service latency and throughput remain unmeasured.

<!-- accuracy-observation -->
```json
""" + _json(summary) + """
```

The complete measured report, configuration and per-document provenance are in
[accuracy-report.json](accuracy-report.json). The selected mock model is
`scripted-fixtures`; its catalog/fixture hashes and absolute runtime directory
are recorded in each model_configuration. A checkout at another path can have
a different processing fingerprint while reproducing the same scalar counts.
There are no optional live observations in this submission.

Reproduce the comparison in its own benchmark database, keeping the paraphrase
separate from the 20-message operator queue:

```bash
env -u API_KEY python3 -m intake --db state/accuracy.sqlite3 accuracy --expected deliverables/accuracy.md --provider mock --output deliverables/accuracy-report.json --operator accuracy-mock
```

`--records PATH` instead compares an existing JSONL measurement without calling
a provider. All 21 oracle documents still contribute to the denominator, so a
file containing only the 20 operator records leaves SYN-001 with zero matches.
Exit code 1 indicates comparison discrepancies. Inspect factual_errors and the
separate checks before repairing source interpretation, prompt or integration.

T43 injects wrong permission, a missing request_date, a false manager and lost
conflicting evidence using `tests/mock_responses/accuracy-wrong.json`. Its
expected scalar result is 249/252; schema, quotes, issues and routing detect
their separate failures. Additional cases cover missing records, failed and
malformed extractions, missing keys expected null, fabricated quotes, duplicate
records, source binding and mixed response-source labels.

## Expected facts and source evidence

"""
    for name, expected in oracle["documents"].items():
        text += f"### {name}\n\n"
        text += f"Source: [{expected['source_file']}](../{expected['source_file']})\n\n"
        text += f"Original source SHA-256: `{expected['source_sha256']}`\n\n"
        text += "```json\n" + _json(expected["scalars"]) + "\n```\n\n"
        text += "Expected route: " + " / ".join(expected["route"]) + ".\n\n"
        fields = list(dict.fromkeys(requirement["field"] for requirement in expected["issue_requirements"]))
        text += "Issue coverage: " + (", ".join(fields) if fields else "no missing/conflicting facts or override issues") + ".\n\n"
        text += "Literal source evidence:\n\n"
        for quote in expected["source_quotes"]:
            text += "\n".join("> " + line for line in quote.split("\n")) + "\n\n"
    return text


def _verification_document(report, intake, evidence) -> str:
    first = report["provenance"][0]
    observations = {"observations": [
        {
            "name": "mock-intake", "response_source": "mock",
            "configuration": {
                "provider": intake["provider"], "model": intake["model"],
                "processing_fingerprint": intake["processing_fingerprint"],
                "policy_sha256": intake["policy_sha256"], "catalog_sha256": intake["catalog_sha256"],
                "workers": intake["workers"],
            },
            "observed": {"records": intake["records"], "status_counts": intake["status_counts"], "matching_source_attempt_links": intake["matching_source_attempt_links"]},
            "evidence": "planning/step-07-cli-run.txt",
        },
        {
            "name": "mock-accuracy", "response_source": "mock",
            "configuration": {
                **first["model_configuration"],
                "prompt_sha256": first["prompt_sha256"], "schema_sha256": first["schema_sha256"],
                "policy_sha256": first["policy_sha256"], "processing_fingerprint": first["processing_fingerprint"],
            },
            "observed": {
                "scalar_correct": report["scalar"]["correct"], "scalar_denominator": report["scalar"]["denominator"],
                "documents": len(report["by_document"]),
                "schema_passed": report["schema"]["passed"], "quote_coverage_passed": report["quotes"]["passed"],
                "issue_coverage_passed": report["issues"]["passed"], "routing_correct": report["routing"]["correct"],
                "factual_errors": len(report["factual_errors"]),
            },
            "evidence": "deliverables/accuracy-report.json",
        },
        {
            "name": "request-example", "response_source": "request-example-only",
            "configuration": evidence["configuration"],
            "observed": evidence["observation"], "evidence": "deliverables/request-evidence.json",
        },
    ]}
    return """# Verification and operator handover

The following are reproducible mock checks and clearly labelled observations.
They are evidence for the tested behavior. At the user's request,
planning/validation.md records an agent evidence audit; independent human review
can use the workflow described in docs/03-implement-and-validate.md.

## Exact operator checks

Activate `.venv` from the repository root first, using the setup in README.md.
Run the block below in a temporary checkout to reproduce the operator workflow.
Each command must exit 0. All commands unset API_KEY, use saved mock responses
or generate request evidence without making a provider call. The verification
queue and the 21-document accuracy benchmark use separate databases.

<!-- runnable-mock-commands:start -->
```bash
env -u API_KEY python3 -m intake --db state/verification.sqlite3 run materials/requests --provider mock --operator verification
env -u API_KEY python3 -m intake --db state/verification.sqlite3 queue
env -u API_KEY python3 -m intake --db state/verification.sqlite3 pending
env -u API_KEY python3 -m intake --db state/verification.sqlite3 history --document IT-001.json
env -u API_KEY python3 -m intake --db state/verification.sqlite3 retry --document IT-001.json --provider mock --operator verification-retry
env -u API_KEY python3 -m intake --db state/verification.sqlite3 resume --provider mock --operator verification-resume
env -u API_KEY python3 -m intake --db state/verification.sqlite3 export --output state/verification-exports
env -u API_KEY python3 -m intake --db state/verification.sqlite3 export --output state/verification-exports --read
env -u API_KEY python3 -m intake --db state/accuracy.sqlite3 accuracy --expected deliverables/accuracy.md --provider mock --output state/verification-accuracy-report.json --operator accuracy-mock
env -u API_KEY python3 -m tools.request_evidence
```
<!-- runnable-mock-commands:end -->

Intended comparisons and assertions:

- Intake/queue: 20 unique document IDs; 11 review and 9 needs-information
  outcomes, with status/owner/next_action checked against the source oracle and
  five policy rules in order. All successful outputs await explicit human review.
- Pending: empty after this successful batch. T37 separately observes queued,
  processing, rate-limit and unavailable work while other results stay readable.
- History/retry: a targeted retry creates a new IT-001 attempt using saved source
  bytes; other documents retain their attempts. T33 checks that a failed retry
  stays current and the earlier success remains in history.
- Resume: completed successes are preserved. T39 kills actual processes, removes
  input files, expires leases, and checks snapshot-based recovery and stale-token
  rejection. No sleep-based throughput claim is inferred from those checks.
- Export: 20 linked current records. Capture the current reference once before
  reading both files. The captured generation fixes source/attempt identity for
  the pair; T42 crashes actual publishers at seven boundaries and verifies an
  old or new complete generation, including a failure before publication.
- Accuracy: 252/252 scalar mock matches; all 21 schema, quote and issue checks
  pass and all 21 routes agree. T43's authored fault input must instead report
  249/252 and explicit discrepancies. These checks do not measure live semantics.
- Request evidence: model/settings and schema agree with the actual request
  builder, authorization is redacted, and provider_call_performed is false.

Run the full acceptance suite separately; T44 executes the operator block above
in a temporary checkout and does not recursively invoke this suite:

```bash
env -u API_KEY python3 -m unittest discover -s tests -v
```

The final full run is saved as planning/test-run.txt when the build checks are
complete; step-specific runs are retained in planning/step-*-test-pass.txt.
Tests block network access and credential reads; the adapter credential test
injects a fixed fixture sentinel instead of reading or setting a real key.
T41 installs pinned packages from prepared local wheels into a fresh environment
on the same Python/platform; upstream downloads and other binary platforms are
outside that offline installation check.

## Recorded observations and configuration

The JSON below is generated from observed output artifacts. The intake
observation comes from the saved step 7 native CLI run. The accuracy observation
comes from actual mock records compared against the independent oracle. The
request example is generated from the builder without an HTTP call. Absolute
mock-directory paths affect configuration fingerprints across checkouts.

<!-- verification-observations -->
```json
""" + _json(observations) + """
```

## Operating assumptions and limits

The supported deployment is one host with a shared local SQLite database in
WAL mode, short transactions, and a common host clock for leases. Multi-host or
network-filesystem operation is unsupported. Export publication uses POSIX
symlinks, filesystem locks, fsync and atomic rename on one local filesystem.
Process-crash checks do not establish hardware power-loss durability.

The business context is 200–400 messages daily and a roughly 2,000-message
burst. T40 exercises 200, 400 and 2,000 authored mock withdrawals with four
workers, stops partway, deletes exports, and resumes without losing completions.
The check asserts unique current records, bounded calls, progress and recovery;
it is not a measured live throughput or service-latency guarantee.

Workers default to four concurrent calls per operator, configurable from 1 to
32; there is no central requests-per-second quota across operators. Active
claims default to 300-second leases renewed every five seconds. Queued sources
have no active lease until a worker starts them. Expired claims are reclaimed
with a new fencing token; stale workers cannot renew or publish. Valid live
leases are skipped during resume. A graceful stop drains in-flight calls and
retains queued work. Unexpected hard interruption leaves whole committed
results or locatable unfinished attempts. Older accepted source completions
can remain in history while the newer revision stays current.

HTTP retry budgets default to three attempts, capped at ten configurable
attempts. Timeouts, connection failures, rate limits and server errors get capped
backoff. Numeric Retry-After values are capped; unsupported/date-form values
fall back to exponential delay. A socket timeout is not a total processing
deadline. Refusal, interruption, malformed JSON, schema and quote failures need
explicit operator retry. Error records preserve actionable categories and
commands without upstream credential-bearing messages.

The policy parser supports destination edits to the supplied five-rule review
template. Changes to rule conditions/order or extraction interpretation require
code/prompt and test updates. Override detection combines finite source patterns
and provider-reported issues; the supplied and derived attacks are tested, not
every possible instruction attack. Source facts and human approval remain
separate. No live model accuracy, provider availability, live throughput or live
rate-limit capacity has been tested or measured in this build.

## Observed repair and external completion

During step 8's initial test-first run, T37 exposed a test acknowledgement race:
a fake-clock advance could make an earlier renewal appear to acknowledge the
new time. The test now samples time once and verifies the durable expiry covers
that lease before signalling. Application lease logic was unchanged by this
test-only repair. See planning/step-08-test-first-initial.txt,
planning/step-08-test-note.txt and the corrected test-first run. Initial missing
accuracy/handover failures were expected before step 8 implementation.

External submission requirements are not performed by these checks:

- R-78: the human/facilitator must verify the coding-session capture is paired
  with the submitted commit. Entire is enabled; user commits create checkpoints,
  but this document does not certify the facilitator's capture submission.
- R-79: commit and push the application, outputs and capture to the repository's
  default branch. The user makes the commits; no push is claimed here.
- R-80: submit only the root repository link through the Classroom assignment.
  No Classroom submission has been performed.

The user-requested agent audit and counts are in planning/validation.md.
Run env -u API_KEY python3 tools/validate_submission.py after the full suite to
recheck saved artifacts and evidence links; it writes planning/validation-checks.json.
Independent reviewers can inspect each marked row's evidence. A passing test is
evidence only for the assertions it actually exercises.
"""


def main() -> None:
    oracle = json.loads((ROOT / "tests/accuracy_expected.json").read_text(encoding="utf-8"))
    validate_oracle(oracle, root=ROOT)
    provider = CatalogProvider(ROOT / "tests/mock_responses")
    config = ProcessingConfig.capture(provider=provider, policy_path=ROOT / "materials/office-policy.md")
    records = run_intake(
        ROOT / "state/accuracy.sqlite3", [ROOT / item["source_file"] for item in oracle["documents"].values()],
        provider_factory=lambda: provider, config=config, operator="accuracy-mock", max_workers=4,
    )
    report = compare(records, oracle, root=ROOT)
    output = ROOT / "deliverables"
    output.mkdir(exist_ok=True)
    (output / "accuracy-report.json").write_text(_json(report) + "\n", encoding="utf-8")
    (output / "accuracy.md").write_text(_accuracy_document(oracle, report), encoding="utf-8")
    request_evidence()
    evidence = json.loads((output / "request-evidence.json").read_text(encoding="utf-8"))
    intake = json.loads((ROOT / "planning/step-07-cli-run.txt").read_text(encoding="utf-8"))
    (output / "verification.md").write_text(_verification_document(report, intake, evidence), encoding="utf-8")
    print(f"Wrote accuracy and verification artifacts: {report['scalar']['correct']}/{report['scalar']['denominator']} scalar mock matches; no live provider call")


if __name__ == "__main__":
    main()
