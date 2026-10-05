"""Function registry with validation for the schema subset used by built-in tools."""
from __future__ import annotations

import copy
import inspect
import json
from typing import Any, Callable


def validate(value: Any, schema: dict, label: str = "arguments") -> None:
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
    kind = schema.get("type")
    if kind in types and type(value) is not types[kind]:
        raise ValueError(f"{label} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{label} must be one of {schema['enum']}")
    if kind == "object":
        properties = schema.get("properties", {})
        missing = set(schema.get("required", [])) - value.keys()
        if missing:
            raise ValueError(f"Missing arguments: {', '.join(sorted(missing))}")
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            raise ValueError("Unexpected arguments")
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], f"{label}.{key}")
    if kind in {"string", "array"}:
        minimum = schema.get("minLength" if kind == "string" else "minItems", 0)
        maximum = schema.get("maxLength" if kind == "string" else "maxItems", float("inf"))
        if not minimum <= len(value) <= maximum:
            raise ValueError(f"{label} has an invalid length")
        if kind == "array":
            for item in value:
                validate(item, schema.get("items", {}), label)
    if kind == "integer" and not schema.get("minimum", float("-inf")) <= value <= schema.get("maximum", float("inf")):
        raise ValueError(f"{label} is outside its allowed range")


def string(**extra) -> dict:
    return {"type": "string", **extra}


def integer(minimum: int, maximum: int, default: int) -> dict:
    return {"type": "integer", "minimum": minimum, "maximum": maximum, "default": default}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, dict] = {}

    def register(self, name: str, description: str, parameters: dict, function: Callable) -> None:
        if not name.isidentifier() or name in self._tools:
            raise ValueError(f"Invalid or duplicate tool name: {name}")
        if parameters.get("type") != "object":
            raise ValueError("Tool parameters must use an object schema")
        if not callable(function):
            raise ValueError("Tool function must be callable")
        self._tools[name] = {"description": description, "parameters": copy.deepcopy(parameters), "function": function}

    def add(self, function: Callable, description: str, properties: dict, required=(), name=None) -> None:
        self.register(name or function.__name__, description, {
            "type": "object", "properties": properties, "required": list(required), "additionalProperties": False,
        }, function)

    def definitions(self) -> list[dict]:
        return copy.deepcopy([
            {"name": name, "description": tool["description"], "parameters": tool["parameters"]}
            for name, tool in self._tools.items()
        ])

    def call(self, name: str, arguments: dict | str) -> Any:
        if name not in self._tools:
            raise KeyError(f"Tool is not enabled: {name}")
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        tool = self._tools[name]
        validate(arguments, tool["parameters"])
        inspect.signature(tool["function"]).bind(**arguments)
        result = tool["function"](**arguments)
        json.dumps(result, allow_nan=False)
        return result
