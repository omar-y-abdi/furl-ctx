"""Version-consistency guards across Furl's distribution surfaces.

Two independent invariants, each enforced here so a drifting version fails CI — and,
in particular, fails the release-please release PR that bumps ``pyproject.toml`` until
the embedded command pins are synced. release-please has **no updater that can rewrite
a version embedded inside a shell-command string** (the ``json`` updater does
full-value replacement at a JSONPath; the ``generic`` updater needs inline
``x-release-please-version`` comments, which strict JSON cannot carry), so this test is
the backstop that keeps the pins honest. See CONTRIBUTING.md "Releasing / version
bumps".

LIBRARY version (``pyproject.toml`` ``project.version``): the ``furl-ctx[mcp]==X.Y.Z``
pins in the PostToolUse hook command (``hooks/hooks.json``) and the MCP server command
(``.mcp.json``) MUST equal it, so ``uv run`` fetches a deterministic wheel instead of
whatever stale resolution its cache happens to hold. The same pin also appears as
worked-example PROSE in ``skills/furl/SKILL.md`` and ``plugins/furl/README.md`` — not
inside machine-read config, so nothing else catches it going stale — and MUST equal it
too (three stale examples slipped past release once already; see CONTRIBUTING.md
"Releasing / version bumps"). The SessionStart status line names the engine alongside
the plugin (``furl X.Y.Z · engine furl-ctx A.B.C``), so its engine half MUST equal it
as well.

PLUGIN version (``plugin.json`` ``version``): the marketplace metadata + entry
versions, the skill frontmatter, and the plugin half of the baked SessionStart
status-line version MUST all equal it, because the plugin cache is version-keyed and
the status line advertises the running plugin build.

Pure stdlib (json, re, tomllib) — no furl_ctx import — so the guard runs even without
the built extension.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import tomllib

_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _ROOT / "pyproject.toml"
_PLUGIN_DIR = _ROOT / "plugins" / "furl"
_HOOKS_JSON = _PLUGIN_DIR / "hooks" / "hooks.json"
_PRETOOL_SCRIPT = _PLUGIN_DIR / "hooks" / "pretool_pipe.py"
_MCP_JSON = _PLUGIN_DIR / ".mcp.json"
_PLUGIN_JSON = _PLUGIN_DIR / ".claude-plugin" / "plugin.json"
_SKILL_MD = _PLUGIN_DIR / "skills" / "furl" / "SKILL.md"
_PLUGIN_README = _PLUGIN_DIR / "README.md"
_MARKETPLACE_JSON = _ROOT / ".claude-plugin" / "marketplace.json"

_SEMVER = r"\d+\.\d+\.\d+"
_PIN_RE = re.compile(r"furl-ctx\[mcp\]==(" + _SEMVER + r")")
# Two capture groups in one match, not two separate regexes: this also pins the
# *relationship* between the halves (plugin version immediately followed by
# "· engine furl-ctx" immediately followed by the engine version), so a status
# line with the right two numbers in the wrong shape still fails loud.
_STATUS_VERSION_RE = re.compile(r"furl (" + _SEMVER + r") · engine furl-ctx (" + _SEMVER + r")")
_FRONTMATTER_VERSION_RE = re.compile(r"^version:\s*(" + _SEMVER + r")\s*$", re.MULTILINE)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _pyproject_version() -> str:
    return str(tomllib.loads(_read(_PYPROJECT))["project"]["version"])


def _plugin_version() -> str:
    return str(json.loads(_read(_PLUGIN_JSON))["version"])


def _hook_command() -> str:
    hooks = json.loads(_read(_HOOKS_JSON))["hooks"]
    return str(hooks["PostToolUse"][0]["hooks"][0]["command"])


def _pretool_command() -> str:
    hooks = json.loads(_read(_HOOKS_JSON))["hooks"]
    return str(hooks["PreToolUse"][0]["hooks"][0]["command"])


def _mcp_command() -> str:
    return " ".join(json.loads(_read(_MCP_JSON))["mcpServers"]["furl"]["args"])


def _session_start_command() -> str:
    hooks = json.loads(_read(_HOOKS_JSON))["hooks"]
    return str(hooks["SessionStart"][0]["hooks"][0]["command"])


def _extract(pattern: re.Pattern[str], text: str, label: str, group: int = 1) -> str:
    match = pattern.search(text)
    assert match is not None, f"no {label} found in: {text!r}"
    return match.group(group)


def _pins_in(text: str) -> list[str]:
    """Every ``furl-ctx[mcp]==X.Y.Z`` pin version mentioned in free-form prose."""
    return _PIN_RE.findall(text)


# --- LIBRARY version: both command pins, prose pins, and the status line's engine
# --- half all == pyproject ---


def test_hook_pin_matches_pyproject_version() -> None:
    assert _extract(_PIN_RE, _hook_command(), "furl-ctx[mcp] pin") == _pyproject_version()


def test_mcp_pin_matches_pyproject_version() -> None:
    assert _extract(_PIN_RE, _mcp_command(), "furl-ctx[mcp] pin") == _pyproject_version()


def test_pretool_pin_matches_pyproject_version() -> None:
    # The opt-in PreToolUse pipe hook resolves the same pinned engine.
    assert _extract(_PIN_RE, _pretool_command(), "furl-ctx[mcp] pin") == _pyproject_version()


def test_pretool_pipe_script_pin_matches_pyproject_version() -> None:
    # pretool_pipe.py BAKES the pin into the rewritten command (the compressor the
    # rewrite pipes through), so it MUST equal pyproject too — a separate pin from
    # the hooks.json command above and, like the prose pins, invisible to
    # release-please's updaters. See _FURL_CTX_PIN in pretool_pipe.py.
    pins = _pins_in(_read(_PRETOOL_SCRIPT))
    assert pins, f"no furl-ctx[mcp]==X.Y.Z pin found in {_PRETOOL_SCRIPT}"
    assert all(pin == _pyproject_version() for pin in pins), (
        f"{_PRETOOL_SCRIPT} pin(s) {pins} != pyproject version {_pyproject_version()!r}"
    )


def test_skill_prose_pin_matches_pyproject_version() -> None:
    pins = _pins_in(_read(_SKILL_MD))
    assert pins, f"no furl-ctx[mcp]==X.Y.Z pin found in {_SKILL_MD}"
    assert all(pin == _pyproject_version() for pin in pins), (
        f"{_SKILL_MD} prose pin(s) {pins} != pyproject version {_pyproject_version()!r}"
    )


def test_plugin_readme_prose_pins_match_pyproject_version() -> None:
    pins = _pins_in(_read(_PLUGIN_README))
    assert pins, f"no furl-ctx[mcp]==X.Y.Z pin found in {_PLUGIN_README}"
    assert all(pin == _pyproject_version() for pin in pins), (
        f"{_PLUGIN_README} prose pin(s) {pins} != pyproject version {_pyproject_version()!r}"
    )


def test_session_start_status_line_engine_version_matches_pyproject_version() -> None:
    version = _extract(
        _STATUS_VERSION_RE, _session_start_command(), "status-line engine version", group=2
    )
    assert version == _pyproject_version()


# --- PLUGIN version: marketplace + skill + status line == plugin.json ---


def test_marketplace_versions_match_plugin_version() -> None:
    market = json.loads(_read(_MARKETPLACE_JSON))
    plugin_version = _plugin_version()
    assert market["metadata"]["version"] == plugin_version
    entries = market["plugins"]
    assert len(entries) == 1
    assert entries[0]["version"] == plugin_version


def test_skill_frontmatter_matches_plugin_version() -> None:
    version = _extract(_FRONTMATTER_VERSION_RE, _read(_SKILL_MD), "skill frontmatter version")
    assert version == _plugin_version()


def test_session_start_status_line_version_matches_plugin_version() -> None:
    version = _extract(_STATUS_VERSION_RE, _session_start_command(), "status-line plugin version")
    assert version == _plugin_version()
