"""How a tool result reaches the model: a delimited block of inert JSON (FR-013, research §8).

``<tool_result name="...">JSON</tool_result>``: the name and the payload are JSON-encoded with
``<``, ``>`` and ``&`` escaped, so nothing inside a result can close the block or open another
one. The payload is the tool envelope (``trust: untrusted_data``) or ``{error, message}``; it never
holds the caller's token.
"""

from ecommerce_ai.agents.assistant.model import ToolResultItem
from ecommerce_ai.agents.prompts.loader import inert_json


def tool_result_text(item: ToolResultItem) -> str:
    """The delimited text of ``item`` sent to the model."""
    return f"<tool_result name={inert_json(item.name)}>{inert_json(item.payload)}</tool_result>"
