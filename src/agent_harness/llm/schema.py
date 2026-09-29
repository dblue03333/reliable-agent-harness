"""Project existing decision/tool contracts into a finite Gemini JSON schema."""

from collections.abc import Sequence
from copy import deepcopy

from agent_harness.models import decision_json_schema


def _project(schema: dict, definitions: dict) -> dict:
    if "$ref" in schema:
        reference = schema["$ref"]
        if not reference.startswith("#/$defs/"):
            raise ValueError("Only local schema references are supported.")
        schema = definitions[reference.removeprefix("#/$defs/")] | {
            key: value for key, value in schema.items() if key != "$ref"
        }
    result = {}
    for key, value in schema.items():
        if key in ("$defs", "discriminator", "title"):
            continue
        if key == "properties":
            # Property names such as incident.title are data, not schema keywords.
            result[key] = {name: _project(child, definitions) for name, child in value.items()}
        elif key in ("oneOf", "anyOf"):
            result["anyOf"] = [_project(child, definitions) for child in value]
        elif key == "const":
            result["enum"] = [value]
        elif key in ("items", "additionalProperties") and isinstance(value, dict):
            result[key] = _project(value, definitions)
        else:
            result[key] = deepcopy(value)
    return result


def gemini_decision_schema(tool_descriptions: Sequence[dict]) -> dict:
    """Specialize generic arguments using the registry's generated input schemas.

    One disjoint branch per registered tool plus final. No handwritten second
    decision contract or unbounded JsonValue object is sent to the provider.
    Local Pydantic and registry validation remain authoritative after generation.
    """
    contract = decision_json_schema()
    definitions = contract["$defs"]
    branches = []
    names = set()
    for description in tool_descriptions:
        name = description["name"]
        if name in names:
            raise ValueError("Duplicate tool description.")
        names.add(name)
        branch = deepcopy(definitions["ToolCallDecision"])
        branch["properties"]["tool"]["enum"] = [name]
        arguments = description["parameters"]
        branch["properties"]["arguments"] = _project(arguments, arguments.get("$defs", {}))
        branches.append(_project(branch, definitions))
    branches.append(_project(definitions["FinalDecision"], definitions))
    # Live Gemini 3.5 Flash-Lite accepted a root anyOf but emitted {}. A required
    # object property keeps the union inside the supported object envelope.
    return {
        "type": "object",
        "properties": {"decision": {"anyOf": branches}},
        "required": ["decision"],
        "additionalProperties": False,
    }
