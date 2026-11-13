"""Build extraction messages while keeping source text in the user role."""
from pathlib import Path


PROMPT_VERSION = "1"
PROMPT_PATH = Path(__file__).with_name("extraction-prompt.txt")
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "snapshot.schema.json"


def build_messages(
    *, message: str, prompt_text: str | None = None, schema_text: str | None = None,
) -> list[dict[str, str]]:
    """Return fresh messages; untrusted text never enters system instructions.

    The HTTP adapter uses this builder. Scripted providers return saved
    envelopes without claiming semantic extraction.
    """
    if not isinstance(message, str):
        raise TypeError("Extraction message must be decoded text")
    instructions = PROMPT_PATH.read_bytes().decode("utf-8") if prompt_text is None else prompt_text
    schema = SCHEMA_PATH.read_bytes().decode("utf-8") if schema_text is None else schema_text
    return [
        {"role": "system", "content": instructions + "\nExtraction JSON Schema:\n" + schema},
        {"role": "user", "content": message},
    ]
