# IT access-request intake

Python 3.11+ application being built in the order in `planning/plan.md`.
Steps 1–8 provide an operator CLI, scripted and HTTP providers, extraction
validation, ordered human-review routing, durable SQLite state, bounded workers,
recovery, consistent file exports and an independent accuracy comparison.
Handover procedures and observations are in `deliverables/verification.md`.
Supported operation is Python 3.11+ on
Linux with a shared local filesystem; exports use POSIX locks and symlinks.

Setup requires Python 3.11+ with the `venv` module and system pip 22.3+.
From the repository root:

```bash
python3 -m venv --without-pip .venv
python3 -m pip --python .venv install -r requirements.lock
. .venv/bin/activate
env -u API_KEY python3 -m unittest discover -s tests -v
```

The environment is created without its own pip so this also works on systems
where `ensurepip` is unavailable. The system pip installs the pinned runtime
dependencies into `.venv`. Installation requires package access or a prepared
offline package source; the tests themselves use saved mock responses, block
network connections and forbid reading `API_KEY`.

Run these commands from the repository root with `.venv` activated. Mock mode
uses only the explicitly saved responses in `tests/mock_responses/catalog.json`:

```bash
env -u API_KEY python3 -m intake run materials/requests --provider mock --operator reviewer-a
env -u API_KEY python3 -m intake queue
env -u API_KEY python3 -m intake pending
env -u API_KEY python3 -m intake history --document IT-001.json
env -u API_KEY python3 -m intake history --document IT-001.json --events
```

The shared database defaults to `state/intake.sqlite3`. Supply `--db PATH`
before or after the command to select another local database. Inspection
commands produce JSON arrays. `run` accepts JSON files or directories and
defaults to four workers. Use `--workers`, `--lease-seconds` and
`--heartbeat-interval` to configure execution. An unchanged rerun reuses
completed successes; `run --force` creates fresh attempts.

Targeted retry uses saved bytes and policy, so the export file can be absent:

```bash
env -u API_KEY python3 -m intake retry --document IT-001.json --provider mock --operator reviewer-a
env -u API_KEY python3 -m intake resume --provider mock --operator reviewer-b
env -u API_KEY python3 -m intake resume --provider mock --retry-errors --operator reviewer-b
```

`retry` forces only the named document. Error records include a shell-quoted
retry command with the actual database and mock directory. Native mock attempts
archive their catalog/fixtures, prompt and schema. Default retry and resume use
saved settings; pass `retry --mock-dir PATH` to deliberately load a repaired
catalog and capture its new hashes. Mock catalogs map decoded message hashes
to explicit reply/error fixtures. Unknown messages produce an actionable mock
error without a live-provider fallback. The source hash still uses original
export bytes independently of that fixture lookup.

Mutation commands return JSON arrays; exit code 0 indicates no extraction
errors, 1 indicates error records, and 2 indicates argument/configuration/I/O
failure. Ctrl-C or SIGTERM stops scheduling, drains in-flight work and leaves
queued work saved, returning 130 or 143. A killed process is recovered after
its active leases expire. Resume infers a single saved provider kind or accepts
`--provider` to select one kind in a mixed queue. Each HTTP attempt restores
its own saved settings and reads `API_KEY` only when making its provider call.

Publish and read a consistent current export:

```bash
env -u API_KEY python3 -m intake export --output deliverables
env -u API_KEY python3 -m intake export --output deliverables --read
```

The first command writes an immutable generation from one SQLite read
transaction, then atomically switches `deliverables/current`. The root files
`deliverables/queue.json` and `deliverables/extractions.jsonl` are relative
symlinks through that reference. Generation manifests include record counts
and SHA-256 digests. Existing handover files, including request evidence, stay
in place. Publication locks serialize writers; source processing can continue.

Readers capture the reference once and open both files under that generation.
The CLI `export --read` performs this protocol. The Python equivalent is:

```python
from pathlib import Path
from intake.exports import capture

snapshot = capture(Path("deliverables"))
queue, extractions = snapshot.read()
```

Opening the two root aliases independently during publication can mix
generations. Captured generations remain available because publication does
not rewrite or delete them. Failed/crashed publication can leave an
unreferenced generation; readers ignore it. Before the first successful
publication, there is no readable snapshot. T42 terminates actual publishers
at seven boundaries and checks that the current pair is wholly old or new.

The committed current files were generated with mock responses using:

```bash
env -u API_KEY python3 -m intake --db state/submission.sqlite3 run materials/requests --provider mock --operator submission-mock
env -u API_KEY python3 -m intake --db state/submission.sqlite3 export --output deliverables
```

`planning/step-07-cli-run.txt` records the commands/configuration and observed
20 records: 11 awaiting review and 9 needing information, with matching source
and attempt links. This is a mock application observation, not measured live
extraction accuracy. SQLite databases and publication lock files are ignored
by Git; the current generation, its manifest and relative references are the
reviewable output artifacts.

Compare the 20 messages plus the authored paraphrase against the source-derived
oracle linked from the accuracy document, using a separate benchmark database:

```bash
env -u API_KEY python3 -m intake --db state/accuracy.sqlite3 accuracy --expected deliverables/accuracy.md --provider mock --output deliverables/accuracy-report.json --operator accuracy-mock
```

The report gives exact scalar counts with a fixed 252-field denominator,
per-field/per-document errors, and separate schema, quote, issue and routing
checks. Missing results remain in the denominator; missing keys never match
expected null. Source hashes bind expectations and measured records to the
same inputs. `--records PATH` compares existing JSONL without calling a
provider. A 20-document file leaves the paraphrase's 12 fields unmatched.

`deliverables/accuracy.md` contains all expected scalar facts and literal source
evidence for the 21 documents. `deliverables/accuracy-report.json` records the
observed mock agreement and configuration; it is not live model accuracy.
`deliverables/verification.md` provides exact mock commands, intended assertions,
observed results/repairs, workload/recovery assumptions and external submission
limits. Regenerate the handover documents from the independent oracle and
actual mock records with:

```bash
env -u API_KEY python3 -m tools.handover
```

This tool preserves the source-authored expectations, writes the measured report
separately, and regenerates credential-redacted request evidence without a live
provider call. Spec-line validation marks remain the human review required by
`docs/03-implement-and-validate.md`.

`intake.providers.ScriptedProvider` consumes saved reply/error fixtures with
optional event barriers for deterministic delays. `intake.extraction.read_export`
preserves the original bytes, SHA-256 and filename and decodes the UTF-8 JSON
message. `intake.extraction.extract_file(path, provider=provider)` passes only
that message to the provider and returns an extraction after validating the
completion envelope, the supplied `snapshot.schema.json` and literal supporting
quotes. Invalid input or output raises a classified `ExtractionError`.

`intake.prompts.build_messages(message=message)` combines the versioned
`intake/extraction-prompt.txt` instructions and supplied schema in the system
role, keeping source text in a separate user message. The HTTP adapter uses
this builder; scripted replies do not perform semantic extraction.

`intake.review.review_file(path, provider=provider)` validates the extraction,
then applies the office policy in order: inspect instruction overrides, confirm
identified dated withdrawals, clarify incomplete or conflicting requests,
review production/write/admin risks, and review complete staging/read requests.
It preserves scalar facts and adds issues for missing facts, identifier
mismatches, invalid dates and reversed access ranges. Extraction and routing
share the same source snapshot. A named manager never supplies approval.

The source override check uses finite text patterns alongside provider-reported
override issues. The mock tests exercise the supplied override and its variant;
they do not establish detection of every possible instruction attack. Other
unresolved extraction issues conservatively require clarification after the
override and withdrawal rules. This lower-level review API propagates
`ExtractionError`; `intake.processing.process_file` converts it to an error
record with null extraction, category, source identity and retry instructions.
`process_files` continues after a failed document. This service is sequential
and stateless; the `StateStore` API below adds durable attempts. Unreadable files
have no source hash; malformed readable exports retain the hash of their
original bytes.

`intake.state.StateStore` uses SQLite WAL on one local host and local filesystem.
It stores original source bytes, document revisions, ingestion sequence,
configuration snapshots, operator identity and completed attempts. Sources,
configuration snapshots, events and completed attempts are retained with
database triggers preventing updates/deletes. Current pointers and pending
claims are stored separately. Provider calls run outside write transactions.

For persistent mock intake from the repository root, after activating `.venv`:

```python
from pathlib import Path
from intake.providers import ScriptedProvider
from intake.state import ProcessingConfig, StateStore

provider = ScriptedProvider(mock_dir=Path("tests/mock_responses"), script=[
    {"reply": "complete-001.json"}, {"reply": "complete-002.json"},
])
config = ProcessingConfig.capture(
    provider=provider, policy_path=Path("materials/office-policy.md"),
)
with StateStore(Path("state/intake.sqlite3")) as store:
    records = store.intake(
        [Path("materials/requests/IT-001.json"), Path("materials/requests/IT-002.json")],
        provider=provider, config=config, operator="operator-a",
    )
    print([(record["document_id"], record["status"]) for record in records])
    print(store.pending())
```

An unchanged success reuses its original attempt without another provider call,
including after reopening the database. Changes to original bytes create a new
source revision; byte-format-only changes count. New source/model/generation/
prompt/schema settings create an extraction attempt. `force=True` bypasses
success reuse; a failed retry becomes current and retains the earlier success
in history. A regular rerun after failure creates another attempt. Later
incremental batches preserve documents absent from that batch, and filenames
remain independent even when messages state the same request ID.

A policy-only refresh reuses the original validated extraction and creates a
routing attempt linked by `extraction_attempt_id` and `routing_attempt_id`.
The supported policy template permits destination edits to the supplied five
review rules; changed conditions/order require routing-code and test changes.
Extraction rules remain in the versioned extraction prompt, so changes to
those rules require updating that prompt and its tests. Policy text alone is
not an executable language for arbitrary new business rules.

`store.current()` returns the current completed result per document.
`store.history(document_id)` returns all completed attempts in acceptance
order, including the current one. `store.pending()` returns unfinished claims
with source/configuration identity, lease owner/expiry and fencing token.
`store.source(attempt_id)` and `store.configuration(attempt_id)` retrieve saved
source and configuration snapshots. Claims and completion commits are separate;
`store.run_claim(claim, provider=provider)` uses saved bytes, prompt/schema,
settings and policy even if their files have subsequently changed or vanished.

State intake requires readable files to accept a durable source revision;
malformed readable exports can be retained as error attempts. Direct
`StateStore.intake` remains sequential. Use `intake.workers.run_intake` for
bounded parallel processing and `intake.workers.resume` for recovery. Both
accept a database path, `provider_factory`, operator name, `max_workers`,
lease duration and optional heartbeat interval. `run_intake` also accepts input
paths and the captured `ProcessingConfig`. Runtime worker limits are recorded
in each started attempt's `execution` metadata.

For parallel processing with the pinned HTTP configuration:

```python
import json
from pathlib import Path
from intake.http_provider import HttpProvider, ProviderSettings
from intake.state import ProcessingConfig
from intake.workers import run_intake, resume

settings = ProviderSettings(**json.loads(Path("config/provider.json").read_text()))
provider = HttpProvider(settings=settings)
config = ProcessingConfig.capture(
    provider=provider, policy_path=Path("materials/office-policy.md"),
)
run_intake(
    Path("state/intake.sqlite3"), sorted(Path("materials/requests").glob("*.json")),
    provider_factory=lambda: HttpProvider(settings=settings), config=config,
    operator="regional-operator-a", max_workers=4,
)
# After an interruption, using the same runtime provider kind:
resume(
    Path("state/intake.sqlite3"), provider_factory=lambda: HttpProvider(settings=settings),
    operator="regional-operator-b", max_workers=4,
)
```

This example makes real provider requests using `API_KEY`. Acceptance tests
instead supply fixture providers or injected HTTP transports. Each provider
factory should return a provider safe for its worker; HTTP providers restore
the model/settings and prompt/schema from the attempt snapshot during resume.

Accepted sources are committed to a durable queue before workers start.
Queued work has no active lease; activation atomically assigns an owner, expiry
and fencing token. Calls default to four workers per operator, configurable
from 1 to 32. Each active call renews its 300-second lease every five seconds
by default, using a separate SQLite connection. Choose a heartbeat interval
shorter than the lease. The shared host clock determines lease expiry.

Two operators can start the same intake: valid active claims are skipped,
completed successes are reused, and queued work is taken once. Concurrency is
bounded per operator; there is no central requests-per-second quota across
operators. HTTP rate limits use the bounded per-call retry/backoff mechanism.
`pending()` exposes `queued`, `processing`, `rate-limit`, `timeout` or
`unavailable` service states with retry timing where applicable. Completed
extraction errors remain visible in `current()` with actionable retry guidance.

Pass a `threading.Event` as `stop_event` for a graceful stop: accepted queued
sources stay durable, no new calls start after the stop is observed, and
in-flight calls drain. An optional `on_result` callback runs after a result is
committed so operators can observe progress while other calls are pending.
After a forced process interruption, resume starts queued work and reclaims
expired active leases from saved source/configuration bytes. It does not steal
a live lease or reprocess completed successes. Use `retry_errors=True` to also
retry latest completed errors. Original files need not still exist.

`store.events(document_id)` preserves claim, start, renewal, abandonment,
reclamation and completion events. A reclaimed worker's old fencing token
cannot publish or renew. A late completion of an older source revision can
remain in history while the current pointer keeps the newer accepted result.
Result publication and the current-pointer update commit together; interruption
before commit leaves pending work, and interruption after commit preserves the
whole result.

The operating context is 200–400 messages daily and roughly 2,000 during a
burst, on one host with a shared local SQLite database. T37–T40 exercise delayed
calls, visible backoff, two actual operator processes, forced worker termination,
crashes around commit, lease recovery, stale workers and source revision races.
The mock workload checks use 200, 400 and 2,000 authored withdrawals with four
workers, stop partway, delete the exports and resume from saved bytes. Each
check asserts one current result per document and preserves earlier completions.
These observed results use scripted fixtures and fake clocks; they establish
the tested state/recovery behavior, not live model accuracy, live service
throughput, hardware power-loss durability or multi-host/network-filesystem
support. T41 additionally exercises native CLI run/inspection/retry/resume,
unknown mock inputs, and installation of the pinned dependencies into a fresh
environment from prepared local wheels on this same interpreter/platform.

The HTTP adapter implements non-streaming Chat Completions with strict JSON
Schema output. The pinned configuration in `config/provider.json` targets
`https://api.openai.com/v1` with `gpt-4o-mini-2024-07-18`, using temperature 0,
top_p 1 and max_tokens 2048. This request format and model snapshot are described
in OpenAI's [Chat Completions reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
and [Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat).
Endpoint, provider label, model, timeout, retry budget/delays and generation
parameters are configurable through `ProviderSettings` and the JSON file.
Only supported generation parameters are accepted; they cannot replace
messages, model or response format. The prompt version is recorded separately.

The real adapter reads its credential only from `API_KEY` when `complete` is
called. It does not store the credential in configuration or evidence. HTTP
redirects are rejected. Logs report document IDs, failure categories and retry
timing without upstream response bodies, raw exceptions, request headers or
credentials. Failure records use fixed category-specific guidance.

Each message gets its own bounded retry budget: at most three attempts by
default, configurable from 1 to 10. Timeouts, connection failures, rate limits
and server failures are retried with capped exponential delays. Numeric
Retry-After values can increase the delay up to the configured cap; invalid or
date-form headers fall back to exponential delay. The default timeout is 30
seconds per socket operation, not an overall processing deadline. Refusal,
interruption, malformed JSON, schema/quote errors and ordinary HTTP client
errors require explicit operator retry. Successful records use `mock` or
`provider/model` response provenance. To use the HTTP adapter through the CLI,
set `API_KEY` in your environment and run:

```bash
python3 -m intake run materials/requests --provider http --config config/provider.json --operator reviewer-a
```

To construct the configured provider for the Python service:

```python
import json
from pathlib import Path
from intake.http_provider import HttpProvider, ProviderSettings

settings = ProviderSettings(**json.loads(Path("config/provider.json").read_text()))
provider = HttpProvider(settings=settings)
# process_file(path, provider=provider) makes real requests using API_KEY.
```

The committed `deliverables/request-evidence.json` is a request example only;
no provider call produced it. Regenerate it without a credential, after
activating `.venv`:

```bash
env -u API_KEY python3 -m tools.request_evidence
```

It is generated from the same request builder used by `HttpProvider.complete`,
with the authorization header redacted, source hash, actual configuration and
implemented response-handling descriptions. T30 compares this evidence with
the request captured by the fixture transport. All recorded acceptance runs
use mock replies or injected HTTP responses; no live model has been tested.

These checks establish mock agreement and structural validity. They do not
measure live model accuracy or decide whether access should be approved.

The challenge is in `CHALLENGE.md`; the staged build and human validation
workflow is in `docs/03-implement-and-validate.md`.
