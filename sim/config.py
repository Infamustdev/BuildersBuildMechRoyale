"""Config / map loading and a tiny JSON-schema checker (no external libraries)."""
import copy
import gzip
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ARENA = os.path.join(ROOT, "arena")
STREAM = os.path.join(ROOT, "check", "stream.jsonl.gz")
SCHEMA = os.path.join(ROOT, "mechfight.schema.json")

_HELLO = None
_SCHEMA = None


def load_hello():
    """The recorded `hello` message (first line of check/stream.jsonl.gz)."""
    global _HELLO
    if _HELLO is None:
        with gzip.open(STREAM, "rt", encoding="utf-8") as f:
            _HELLO = json.loads(f.readline())
    return copy.deepcopy(_HELLO)


def load_config():
    """Deep copy of the real `hello.config`."""
    return load_hello()["config"]


def load_worldmap(step_up=None):
    """Build the shared WorldMap (worldmap.py at the repo root)."""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from worldmap import WorldMap
    if step_up is None:
        step_up = load_config()["mech"]["step_up"]
    return WorldMap(ARENA, step_up=step_up)


def load_schema():
    global _SCHEMA
    if _SCHEMA is None:
        with open(SCHEMA, "r", encoding="utf-8") as f:
            _SCHEMA = json.load(f)
    return _SCHEMA


# ---------------------------------------------------------------- mini schema validator
def _is_type(v, t):
    if t == "object":
        return isinstance(v, dict)
    if t == "array":
        return isinstance(v, list)
    if t == "string":
        return isinstance(v, str)
    if t == "boolean":
        return isinstance(v, bool)
    if t == "null":
        return v is None
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    return True


def schema_validate(inst, schema=None, root=None, path="$"):
    """Returns a list of error strings (empty = valid). Supports the subset the schema uses."""
    root = root or load_schema()
    if schema is None:
        schema = {"$ref": "#/definitions/Observation"}
    errs = []
    if "$ref" in schema:
        ref = schema["$ref"]
        node = root
        for part in ref.lstrip("#/").split("/"):
            node = node[part]
        return schema_validate(inst, node, root, path)
    if "anyOf" in schema:
        for sub in schema["anyOf"]:
            if not schema_validate(inst, sub, root, path):
                return []
        return [f"{path}: matches no anyOf branch"]
    if "oneOf" in schema:
        ok = sum(1 for sub in schema["oneOf"] if not schema_validate(inst, sub, root, path))
        return [] if ok >= 1 else [f"{path}: matches no oneOf branch"]
    if "const" in schema and inst != schema["const"]:
        errs.append(f"{path}: expected const {schema['const']!r}, got {inst!r}")
    if "enum" in schema and inst not in schema["enum"]:
        errs.append(f"{path}: {inst!r} not in enum")
    t = schema.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        if not any(_is_type(inst, x) for x in ts):
            errs.append(f"{path}: expected {t}, got {type(inst).__name__}")
            return errs
    if isinstance(inst, dict):
        for k in schema.get("required", []):
            if k not in inst:
                errs.append(f"{path}: missing required '{k}'")
        props = schema.get("properties", {})
        addl = schema.get("additionalProperties", True)
        for k, v in inst.items():
            if k in props:
                errs += schema_validate(v, props[k], root, f"{path}.{k}")
            elif isinstance(addl, dict):
                errs += schema_validate(v, addl, root, f"{path}.{k}")
    elif isinstance(inst, list):
        if "minItems" in schema and len(inst) < schema["minItems"]:
            errs.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(inst) > schema["maxItems"]:
            errs.append(f"{path}: more than {schema['maxItems']} items")
        it = schema.get("items")
        if isinstance(it, dict):
            for i, v in enumerate(inst):
                errs += schema_validate(v, it, root, f"{path}[{i}]")
    return errs
