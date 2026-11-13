"""Build extraction messages while keeping source text in the user role."""
from pathlib import Path


PROMPT_VERSION = "1"
PROMPT_PATH = Path(__file__).with_name("extraction-prompt.txt")
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "snapshot.schema.json"


def build_messages(*, message: str) -> list[dict[str, str]]:
    """Return fresh messages; untrusted text never enters system instructions.

    The HTTP adapter will use this builder in the next build step. Scripted
    providers return saved envelopes without claiming semantic extraction.
    """
    if not isinstance(message, str):
        raise TypeError("Extraction message must be decoded text")
    instructions = PROMPT_PATH.read_text(encoding="utf-8")
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    return [
        {"role": "system", "content": instructions + "\nExtraction JSON Schema:\n" + schema},
        {"role": "user", "content": message},
    ]
