# IT access-request intake

Python 3.11+ application being built in the order in `planning/plan.md`.
Steps 1–4 provide scripted and HTTP providers, extraction validation, ordered
routing for human review, and independent processing with explicit failure
records. The operator CLI, persistence and parallel workers are later steps.

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
`process_files` continues after a failed document. Processing is currently
sequential and has no durable attempt state. Unreadable files have no source
hash; malformed readable exports retain the hash of their original bytes.

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
errors require explicit operator retry. Retry strings reserve the CLI interface
planned for step 7; until then, rerun `process_file` using the same provider
settings. Successful records use `mock` or `provider/model` response provenance.

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
