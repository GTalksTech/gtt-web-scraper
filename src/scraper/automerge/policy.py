"""Which changes weaken the checks (ADR 0002): a pull request making any of them never
merges itself, however its tests went, because a failure can be made to pass by
weakening the test. The fixing agent may add Canary URLs, Source List domains and
corpus entries, and tighten thresholds; it may not:

- remove a Canary Set entry, or loosen one (change what it expects, drop its reason,
  change or add a forced Fetcher or Search Provider);
- remove a domain from the Source List's trusted or blocked domains (moving one from
  blocked to trusted removes it from blocked);
- remove or relabel a Content Gate corpus entry, or remove or replace its page;
- loosen a Content Gate or health threshold in config/scraper.yaml, or remove a
  marker (any string the Gate's code matches on) from src/scraper/gate.py;
- change what decides merging: the workflows, or this package;
- weaken the test suite itself: delete a test file or one of the suite's scripts,
  change tests/conftest.py or how pytest and mypy run (pyproject.toml), or point the
  service at another Canary Set or Source List file (src/scraper/config.py).

Editing or adding tests is ordinary work and isn't held; neither is a logic change to
the Gate, which the corpus has to pass.

Each rule compares a file before and after the pull request; nothing here runs code
from the pull request.
"""

from __future__ import annotations

import ast
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import yaml

CANARY_SET = "config/canary.yaml"
SOURCE_LIST = "config/source-list.yaml"
SETTINGS = "config/scraper.yaml"
CORPUS = "tests/corpus/corpus.yaml"
CORPUS_PAGES = "tests/corpus/pages/"
GATE = "src/scraper/gate.py"
# Changing these changes what merges, so they never merge themselves.
GUARDS = (".github/", "src/scraper/automerge/", "tests/test_automerge.py", "tests/test_gate_corpus.py")

CONFIG_CODE = "src/scraper/config.py"
PYPROJECT = "pyproject.toml"
# Every test, so deleting one is held; tests.yml runs all of these.
SUITE = ("tests/", "scripts/setup/test-wizard.sh", "deploy/pi5/test-deploy.sh")
# Changing this changes every test.
HARNESS = ("tests/conftest.py",)
# The names in config.py of the files the policy guards.
GUARDED_FILES = ("CANARY_SET_FILE", "SOURCE_LIST_FILE")
# pyproject.toml tables that say what the suite runs.
SUITE_SETTINGS = ("pytest", "mypy")

# Thresholds and the direction that loosens them: -1 when lowering loosens, +1 when
# raising does.
THRESHOLDS: dict[tuple[str, str], int] = {
    ("gate", "min_chars"): -1,
    ("gate", "accepted_chars"): -1,
    ("health", "min_success_rate"): -1,
    ("health", "min_attempts"): +1,
}
# A Canary entry may gain a reason (that pins it tighter); any other field it gains or
# changes loosens it.
_CANARY_FIELDS_ADDABLE = ("reason",)


@dataclass(frozen=True)
class Change:
    """One file a pull request changes: its contents before (None if it adds the file)
    and after (None if it deletes it). Only files `watched` needs real contents."""

    path: str
    before: bytes | None
    after: bytes | None


def watched(path: str) -> bool:
    """Whether a rule reads this file's contents (so the caller fetches them)."""
    return path in _RULES or path.startswith(CORPUS_PAGES)


def weakenings(changes: list[Change]) -> list[str]:
    """How these changes weaken the checks, one line each; empty if they don't."""
    found: list[str] = []
    for change in changes:
        if change.path.startswith(GUARDS):
            found.append(f"changes `{change.path}`, which decides what merges")
            continue
        if change.path in HARNESS:
            found.append(f"changes `{change.path}`, which sets up every test")
            continue
        rule = _RULES.get(change.path)
        if rule is not None:
            found += rule(change)
        elif change.path.startswith(CORPUS_PAGES):
            if change.before is not None:
                name = change.path.removeprefix(CORPUS_PAGES)
                found.append(f"removes or replaces Content Gate corpus page `{name}`")
        elif change.after is None and change.path.startswith(SUITE):
            found.append(f"deletes `{change.path}`, part of the test suite")
    return found


# ── the rules ──────────────────────────────────────────────────────────────


def _canary(change: Change) -> list[str]:
    before = _canary_entries(change.before)
    after = _canary_entries(change.after)
    if after is None:
        return [f"makes `{CANARY_SET}` unreadable"]
    found = []
    for key, entry in (before or {}).items():
        what = f"Canary URL `{key[1]}`" if key[0] == "url" else f"Canary search `{key[1]}`"
        now = after.get(key)
        if now is None:
            found.append(f"removes {what}")
            continue
        loosened = [
            f"{name}: {entry.get(name) or 'none'} → {now.get(name) or 'none'}"
            for name in sorted(set(entry) | set(now))
            if entry.get(name) != now.get(name)
            and not (name in _CANARY_FIELDS_ADDABLE and name not in entry)
        ]
        if loosened:
            found.append(f"loosens {what} ({'; '.join(loosened)})")
    return found


def _canary_entries(raw: bytes | None) -> dict[tuple[str, str], dict[str, Any]] | None:
    """Canary entries by ("url", url) or ("query", query); None if unreadable."""
    items = _yaml(raw, default=[])
    if not isinstance(items, list):
        return None
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        field = "url" if item.get("url") else "query"
        entries[(field, str(item.get(field)))] = {k: _str(v) for k, v in item.items()}
    return entries


def _source_list(change: Change) -> list[str]:
    before = _yaml(change.before, default={})
    after = _yaml(change.after, default={})
    if not isinstance(after, dict):
        return [f"makes `{SOURCE_LIST}` unreadable"]
    found = []
    for standing in ("trusted", "blocked"):
        kept = _domains(after.get(standing))
        for domain in sorted(_domains(before.get(standing) if isinstance(before, dict) else None)):
            if domain not in kept:
                found.append(f"removes `{domain}` from the Source List's {standing} domains")
    return found


def _domains(items: Any) -> set[str]:
    return {str(d).strip().lower() for d in items or [] if d} if isinstance(items, list) else set()


def _corpus(change: Change) -> list[str]:
    before = _named(_yaml(change.before, default=[]))
    after = _named(_yaml(change.after, default=[]))
    found = []
    for name, entry in before.items():
        if name not in after:
            found.append(f"removes Content Gate corpus entry `{name}`")
        elif after[name] != entry:
            found.append(f"relabels Content Gate corpus entry `{name}`")
    return found


def _named(items: Any) -> dict[str, Any]:
    if not isinstance(items, list):
        return {}
    return {str(i["name"]): i for i in items if isinstance(i, dict) and i.get("name")}


def _settings(change: Change) -> list[str]:
    before = _yaml(change.before, default={})
    after = _yaml(change.after, default={})
    if not isinstance(after, dict):
        return [f"makes `{SETTINGS}` unreadable"]
    found = []
    for (section, name), loosening in THRESHOLDS.items():
        was = _number(before, section, name)
        now = _number(after, section, name)
        if was is None:
            continue
        if now is None:
            found.append(f"removes {section}.{name} (was {was:g})")
        elif (now - was) * loosening > 0:
            found.append(f"loosens {section}.{name} ({was:g} → {now:g})")
    return found


def _number(settings: Any, section: str, name: str) -> float | None:
    value = settings.get(section, {}).get(name) if isinstance(settings, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _gate(change: Change) -> list[str]:
    before = _strings(change.before)
    after = _strings(change.after)
    if before is None:
        return []
    if after is None:
        return [f"makes `{GATE}` unreadable"]
    return [f"removes Content Gate marker `{s}`" for s in sorted(before - after)]


def _strings(raw: bytes | None) -> set[str] | None:
    """The string literals in Python source, leaving out docstrings and the text of
    f-strings (messages, not markers); None if it doesn't parse."""
    if raw is None:
        return set()
    try:
        tree = ast.parse(raw)
    except (SyntaxError, ValueError):
        return None
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            skip.update(id(v) for v in node.values)
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            skip.add(id(body[0].value))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
    }


def _pyproject(change: Change) -> list[str]:
    before = _toml(change.before).get("tool", {})
    after = _toml(change.after).get("tool", {})
    return [
        f"changes how the tests or typecheck run (`[tool.{table}]` in {PYPROJECT})"
        for table in SUITE_SETTINGS
        if before.get(table) != after.get(table)
    ]


def _toml(raw: bytes | None) -> dict[str, Any]:
    try:
        return tomllib.loads(raw.decode()) if raw else {}
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}


def _config_code(change: Change) -> list[str]:
    before = _assigned(change.before)
    after = _assigned(change.after)
    return [
        f"changes which file holds the Canary Set or Source List (`{name}`)"
        for name in GUARDED_FILES
        if before.get(name) != after.get(name)
    ]


def _assigned(raw: bytes | None) -> dict[str, str]:
    """Module-level `NAME = "text"` assignments in Python source."""
    try:
        tree = ast.parse(raw or b"")
    except (SyntaxError, ValueError):
        return {}
    return {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


# ── reading ────────────────────────────────────────────────────────────────


def _yaml(raw: bytes | None, default: Any) -> Any:
    """Parsed YAML; `default` for a missing (or empty) file, None if unreadable."""
    if raw is None:
        return default
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
    return default if loaded is None else loaded


def _str(value: Any) -> str:
    return "" if value is None else str(value)


_RULES: dict[str, Callable[[Change], list[str]]] = {
    CANARY_SET: _canary,
    SOURCE_LIST: _source_list,
    CORPUS: _corpus,
    SETTINGS: _settings,
    GATE: _gate,
    PYPROJECT: _pyproject,
    CONFIG_CODE: _config_code,
}
