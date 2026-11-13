# IT access-request intake

Python 3.11+ application being built in the order in `planning/plan.md`.
Steps 1 and 2 provide a scripted offline provider and an extraction service.
Review routing, the operator CLI, persistence and the real provider adapter
are later steps.

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

These checks establish mock agreement and structural validity. They do not
measure live model accuracy or decide whether access should be approved.

The challenge is in `CHALLENGE.md`; the staged build and human validation
workflow is in `docs/03-implement-and-validate.md`.
