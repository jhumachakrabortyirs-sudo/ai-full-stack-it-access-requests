# IT access-request intake

Python 3.11+ application being built in the order in `planning/plan.md`.
Steps 1–3 provide a scripted offline provider, an extraction service, extraction
instructions and ordered routing for human review. The operator CLI, persistence
and the real provider adapter are later steps.

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
role, keeping source text in a separate user message. The HTTP adapter will use
this builder in step 4; scripted replies do not perform semantic extraction.

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
override and withdrawal rules. Extraction failures still propagate as
`ExtractionError`; failure queue records and retries are step 4 work.

These checks establish mock agreement and structural validity. They do not
measure live model accuracy or decide whether access should be approved.

The challenge is in `CHALLENGE.md`; the staged build and human validation
workflow is in `docs/03-implement-and-validate.md`.
