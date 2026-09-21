"""PROVISIONAL RCO placeholder skill-format adapter (Gate 1). NOT the real RCO skill format.

The real format (including any alignment with either app's own SKILL.md format) is the operator's later
decision. This adapter exists only so that discovery is testable. The hub core knows nothing of this
format: it imports this module by the name in rco.toml ([skills] adapter), globs PATTERN, calls
describe(path, data) and reads only the returned "name".
"""

import re
import tomllib
from pathlib import Path
from typing import Any

PATTERN = "*.skill.toml"
FORMAT = "rco-provisional-0"
MAX_BYTES = 64 * 1024

# key -> (type, required); list types are lists of strings
FIELDS: dict[str, tuple[type, bool]] = {
    "format": (str, True),
    "name": (str, True),
    "description": (str, True),
    "when_it_fits": (str, False),
    "inputs": (list, False),
    "outputs": (list, False),
    "good_result": (str, False),
    "model_preference": (str, False),
    "acts_on_world": (bool, False),
    "may_touch": (list, False),
    "agents": (dict, False),  # agent name -> instructions: the named roles a turn takes as <skill>:<agent>
}
AGENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")  # the hub's role-word syntax


class Rejected(Exception):
    """The file is not a valid PROVISIONAL skill; str(e) is the reason."""


def describe(path: Path, data: bytes) -> dict[str, Any]:
    if len(data) > MAX_BYTES:
        raise Rejected(f"too large: {len(data)} bytes (max {MAX_BYTES})")
    try:
        text = data.decode("utf-8").removeprefix("﻿")
    except UnicodeDecodeError:
        raise Rejected("not UTF-8") from None
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise Rejected(f"not TOML: {e}") from None
    if doc.get("format") != FORMAT:
        raise Rejected(f'missing or wrong format marker (expected format = "{FORMAT}")')
    for key in doc:
        if key not in FIELDS:
            raise Rejected(f"unknown key '{key}'")
    for key, (typ, required) in FIELDS.items():
        if key not in doc:
            if required:
                raise Rejected(f"missing required key '{key}'")
            continue
        value = doc[key]
        if not isinstance(value, typ) or (typ is list and not all(isinstance(v, str) for v in value)) or (
            typ is dict and not all(AGENT_RE.fullmatch(k) and isinstance(v, str) and v.strip() for k, v in value.items())
        ):
            want = {list: "a list of strings", dict: 'a table of agent-name = "instructions"'}.get(typ, typ.__name__)
            raise Rejected(f"'{key}' must be {want}")
    for key in ("name", "description"):
        if not doc[key].strip():
            raise Rejected(f"'{key}' must not be empty")
    doc.setdefault("model_preference", "either")
    return doc


def agents(doc: dict[str, Any]) -> list[str]:
    """The named agent roles a turn can take within this skill, in file order."""
    return list(doc.get("agents") or {})


def brief(doc: dict[str, Any], agent: str | None = None) -> str:
    """The skill as a work instruction for a turn's packet, with the named agent's part when the turn gives one.
    The hub core passes this text on unread. Routing data (model_preference) stays out, because packets never name
    a model."""
    parts = [doc["description"].strip()]
    if doc.get("when_it_fits"):
        parts.append(f"When it fits: {doc['when_it_fits'].strip()}")
    for key, title in (("inputs", "Inputs"), ("outputs", "Produce")):
        if doc.get(key):
            parts.append(f"{title}:\n" + "\n".join(f"- {v.strip()}" for v in doc[key]))
    if doc.get("good_result"):
        parts.append(f"A good result: {doc['good_result'].strip()}")
    if doc.get("acts_on_world") is False:
        parts.append("This skill only reasons over text: change no files and run nothing that acts on the world.")
    elif doc.get("acts_on_world") is True:
        touch = ", ".join(doc.get("may_touch") or []) or "nothing named"
        parts.append(f"This skill may act on the world, only on: {touch}.")
    table = doc.get("agents") or {}
    if table:
        parts.append("Agents in this skill: " + ", ".join(table) + ".")
    if agent is not None:
        instructions = table.get(agent)
        parts.append(f"Your agent: {agent}\n" + (
            instructions.strip() if instructions
            else "This skill declares no instructions for this agent: do what its name says, as the operator's note directs."
        ))
    return "\n".join(parts)
