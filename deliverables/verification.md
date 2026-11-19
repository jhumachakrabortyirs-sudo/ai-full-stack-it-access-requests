# Verification and operator handover

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
{
  "observations": [
    {
      "name": "mock-intake",
      "response_source": "mock",
      "configuration": {
        "provider": "mock",
        "model": "scripted-fixtures",
        "processing_fingerprint": "63f87fd4527fc6020671e13d43fd086b3441d5135a5a417f2c1a045f62bdf0c0",
        "policy_sha256": "b949596765aef45377d9f8d0792f9113109b1f811ef4f3be26919c162e1f631a",
        "catalog_sha256": "b5b19fa107d04614253ca8e1519ab10abe3738bbe64392a311f4918c0aec1d0e",
        "workers": 4
      },
      "observed": {
        "records": 20,
        "status_counts": {
          "review": 11,
          "needs-information": 9
        },
        "matching_source_attempt_links": true
      },
      "evidence": "planning/step-07-cli-run.txt"
    },
    {
      "name": "mock-accuracy",
      "response_source": "mock",
      "configuration": {
        "catalog_sha256": "b5b19fa107d04614253ca8e1519ab10abe3738bbe64392a311f4918c0aec1d0e",
        "fixture_sha256": {
          "changed-001.json": "e5562247838d26ad203ed05b86c59bd5f7da54aae38dcfb933fcf9dea42943eb",
          "complete-001.json": "4f52250b1a643d07045e1a4078c5b08b3011206d90917d2f1dfcda05c74c46e3",
          "complete-002.json": "a82ad888e970f12a484406bf2ca91e8141bd2e972b9908176dd532d90feafea3",
          "complete-003.json": "2b6c2e55a97332d27e6bb8913c2212427dc1fade194d89c04da40b905f9fb035",
          "complete-004.json": "c450f8be7b033b965c19c7845030535c0608cacb97b9dc7b1a07db41a58889b5",
          "complete-005.json": "4ad232700bb11e46a671e461e88e105a3b6836e1b4825f53fa9d7f7bc1d48188",
          "complete-006.json": "4afa837ac53088ab19d744f0722e872c19e430ccf3947f9b318c705d3e39cb1d",
          "complete-007.json": "21f86f0c0e91d9f649ff441d921f1269e8867638c2e33f88119f94c035069e42",
          "complete-008.json": "292f2b110600b9deb3d405623432ce259c194505c40566633fc0a981b8e40c36",
          "complete-009.json": "761ba4992895a0c056f362c221537d17250134db0f0f17a1f7d4fd44cd14810d",
          "complete-010.json": "bf6c434b1d22b4d4131bf54465d11b5422f6ca01d573cd5bf58fe7aa3f8d928a",
          "complete-011.json": "e172a2898da5310af1ca2d2cad36624a5433ec00678af7b70236f97c841a1a33",
          "complete-012.json": "352848caa56cd1922206c00ae17b6632537e05a3ea35e77b6394698bca47a2f0",
          "complete-013.json": "e811c44c174389b3e810fc3bc632179b90c7c3e1e20a4f9c53dbad0fc9ed8918",
          "complete-014.json": "ddd9e60d78821d20e40dc96f85f54f6844798a89aecb2be689b82387fb6b8624",
          "complete-015.json": "ac1c2702b539747c668dec470e505695bc4530316c7860374ec5b33462b8b1c8",
          "complete-016.json": "e5b7b3819027bb6c5cb00923700136dfb3cd4373431b8a74a333e6d53c7fc380",
          "complete-017.json": "73be802c8200294a87592965ca3b5e4973d8478d9279d7d747abfcc5bf694035",
          "complete-018.json": "b33cfa09972ec9a35c46bff6091e4dd9d2933ce45c90b991f4aed7d49b452ef9",
          "complete-019.json": "af68e9ec3d78ba58de67da4f3126966f352b55daf495f33fc1d91441f4b2f39c",
          "complete-020.json": "4022d4833aa0dab8e130e87e3fb7d1aaeaf5be744e9e8650c82c0be0d152fe73",
          "synthetic.json": "34ea1e089ba108d23ec61e64641df04ca1bdf36831a85cb7ccb7eb4ef0ddac2d",
          "thread-withdrawal.json": "8054c07dbceae807718a9d2e18e849e7de2ab12d81e6c4c44a926cc6ccbe38db"
        },
        "generation": {},
        "mock_dir": "/home/user/Project/ai-full-stack-it-access-requests/tests/mock_responses",
        "model": "scripted-fixtures",
        "prompt_version": "1",
        "provider": "mock",
        "prompt_sha256": "ffbaa49c4841fe1516983c18c24a5be5a59ea360ca44a8024d5fa7ee5ca36043",
        "schema_sha256": "2802bed75be35046552b6d23f45447b416b440fc8ba5a62a9d1d50d0eebbc636",
        "policy_sha256": "b949596765aef45377d9f8d0792f9113109b1f811ef4f3be26919c162e1f631a",
        "processing_fingerprint": "63f87fd4527fc6020671e13d43fd086b3441d5135a5a417f2c1a045f62bdf0c0"
      },
      "observed": {
        "scalar_correct": 252,
        "scalar_denominator": 252,
        "documents": 21,
        "schema_passed": 21,
        "quote_coverage_passed": 21,
        "issue_coverage_passed": 21,
        "routing_correct": 21,
        "factual_errors": 0
      },
      "evidence": "deliverables/accuracy-report.json"
    },
    {
      "name": "request-example",
      "response_source": "request-example-only",
      "configuration": {
        "base_url": "https://api.openai.com/v1",
        "provider": "openai",
        "model": "gpt-4o-mini-2024-07-18",
        "timeout": 30.0,
        "max_attempts": 3,
        "retry_base_delay": 1.0,
        "retry_max_delay": 30.0,
        "generation": {
          "temperature": 0,
          "top_p": 1,
          "max_tokens": 2048
        },
        "prompt_version": "1"
      },
      "observed": {
        "kind": "request-example-only",
        "provider_call_performed": false
      },
      "evidence": "deliverables/request-evidence.json"
    }
  ]
}
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
