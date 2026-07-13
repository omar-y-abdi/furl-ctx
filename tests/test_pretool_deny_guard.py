"""Deny/ask-aware guard on the PreToolUse pipe (reviewer-84 F3 — SECURITY).

CORE PROPERTY (non-negotiable): the pipe must NEVER rewrite a command that
Claude Code would subject to a permissions **deny** or **ask** rule. Claude Code
evaluates permission rules against the REWRITTEN command, and the furl-pipe
wrapper no longer matches ``Bash(verb:*)`` patterns — so rewriting a denied
command silently downgrades a hard deny to "ask" (normal mode) or trips the
obfuscation classifier (auto mode). When in ANY doubt — unreadable or malformed
settings, unparseable command, compound command, glob rules we cannot interpret
— the hook must PASS THROUGH (emit nothing) so the original command runs and
the deterministic rule fires. Fail toward no-compression, never toward masking
a permission rule.

Unit tests feed the loader/matcher/decision functions synthetic settings and
commands (no live Claude Code needed). Acceptance tests run the real hook as a
subprocess against settings files on disk — one per review-84 G3 scenario, each
with a hermetic HOME so the developer's real ``~/.claude`` can never leak in.
Pre-guard, the hook rewrote in every deny/ask scenario below (the assert-empty
pins failed), which is the pre-fix proof for this round.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

_HOOKS = Path(__file__).resolve().parents[1] / "plugins" / "furl" / "hooks"
_PRETOOL = _HOOKS / "pretool_pipe.py"

_spec = importlib.util.spec_from_file_location("_furl_pretool_pipe_guard", _PRETOOL)
assert _spec is not None and _spec.loader is not None
_pretool_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pretool_mod)


# --- unit: rule loader ------------------------------------------------------------


def _write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_loader_collects_bash_deny_and_ask_only(tmp_path) -> None:
    """Bash-governing entries from BOTH deny and ask are collected; other tools'
    rules (and BashOutput, which merely shares the prefix) are irrelevant."""
    path = tmp_path / "settings.json"
    _write_settings(
        path,
        {
            "permissions": {
                "deny": ["Bash(printf:*)", "WebFetch", "BashOutput(x)", "Read(/etc/*)"],
                "ask": ["Bash(curl:*)"],
                "allow": ["Bash(rm -rf /:*)"],  # allow entries must NOT gate the pipe
            }
        },
    )
    bodies, doubt = _pretool_mod._load_bash_rule_bodies((path,))
    assert doubt is False
    assert bodies == ["printf:*", "curl:*"]


def test_loader_blanket_and_unparseable_bash_entries(tmp_path) -> None:
    """A bare ``Bash`` rule and a malformed ``Bash(...`` entry are BLANKET
    (body ``None``): they could govern anything, so everything passes through."""
    path = tmp_path / "settings.json"
    _write_settings(path, {"permissions": {"deny": ["Bash", "Bash(git push"]}})
    bodies, doubt = _pretool_mod._load_bash_rule_bodies((path,))
    assert doubt is False
    assert bodies == [None, None]


def test_loader_missing_file_is_not_doubt(tmp_path) -> None:
    bodies, doubt = _pretool_mod._load_bash_rule_bodies((tmp_path / "absent.json",))
    assert (bodies, doubt) == ([], False)


def test_loader_doubt_on_malformed_sources(tmp_path) -> None:
    """Anything that PREVENTS knowing the rules is doubt → passthrough: invalid
    JSON, a non-dict document, a non-dict permissions block, a non-list deny/ask
    array, or a non-string entry."""
    cases = [
        "{not json",
        json.dumps(["not", "a", "dict"]),
        json.dumps({"permissions": "nope"}),
        json.dumps({"permissions": {"deny": "Bash(printf:*)"}}),
        json.dumps({"permissions": {"ask": [42]}}),
    ]
    for i, text in enumerate(cases):
        path = tmp_path / f"settings-{i}.json"
        path.write_text(text, encoding="utf-8")
        _bodies, doubt = _pretool_mod._load_bash_rule_bodies((path,))
        assert doubt is True, f"case {i} must raise doubt: {text!r}"


def test_loader_merges_all_settings_files(tmp_path) -> None:
    a = tmp_path / "a" / "settings.json"
    b = tmp_path / "b" / "settings.local.json"
    _write_settings(a, {"permissions": {"deny": ["Bash(touch:*)"]}})
    _write_settings(b, {"permissions": {"ask": ["Bash(curl:*)"]}})
    bodies, doubt = _pretool_mod._load_bash_rule_bodies((a, b))
    assert doubt is False
    assert sorted(str(x) for x in bodies) == ["curl:*", "touch:*"]


# --- unit: matcher ----------------------------------------------------------------


def _matches(command: str, body: str | None) -> bool:
    verb = command.split()[0]
    return bool(_pretool_mod._rule_matches_command(command, verb, body))


def test_matcher_prefix_rule_semantics() -> None:
    assert _matches("printf 'X\\n'", "printf:*") is True
    assert _matches("printf", "printf:*") is True
    assert _matches("echo hi", "printf:*") is False
    # Raw prefix semantics: Bash(git:*) governs gitk-style continuations too.
    assert _matches("gitk --all", "git:*") is True
    # A longer rule prefix does not govern a shorter different command.
    assert _matches("git status", "git-lfs:*") is False


def test_matcher_same_verb_is_conservatively_matched() -> None:
    """DOCUMENTED over-match: ``Bash(git push:*)`` + ``git status`` passes
    through. Same-verb commands are never rewritten — over-matching only costs
    savings; under-matching would mask a rule."""
    assert _matches("git status", "git push:*") is True


def test_matcher_exact_and_bare_verb_rules() -> None:
    assert _matches("printf 'X'", "printf 'X'") is True  # exact form
    assert _matches("printf 'Y'", "printf 'X'") is True  # same verb → conservative
    assert _matches("printf hello", "printf") is True  # bare verb rule
    assert _matches("echo hi", "printf 'X'") is False


def test_matcher_blanket_and_glob_rules_always_match() -> None:
    assert _matches("anything at all", None) is True  # bare Bash
    assert _matches("anything at all", "*") is True
    assert _matches("anything at all", ":*") is True
    assert _matches("git push", "gi*:*") is True  # glob verb prefix
    assert _matches("kubectl get", "k*l:*") is True  # uninterpretable glob → match


# --- unit: decision ---------------------------------------------------------------


def test_decision_zero_rules_always_rewrites() -> None:
    """The common zero-config case (fresh install, no deny/ask Bash rules):
    nothing can be masked, so even compound commands rewrite — v11's
    zero-config savings are preserved."""
    guard = _pretool_mod._deny_guard_passthrough
    assert guard("cat bigfile", []) is False
    assert guard("printf x | tee y && ls; echo done", []) is False


def test_decision_compound_with_any_rule_passes_through() -> None:
    """With ANY deny/ask Bash rule present, a command containing a construct
    that can introduce another command is never parsed segment-by-segment —
    it passes through wholesale."""
    guard = _pretool_mod._deny_guard_passthrough
    rules = ["unrelated-verb:*"]
    for command in (
        "printf x | tee y",
        "echo a && echo b",
        "echo a || echo b",
        "echo a; echo b",
        "echo $(whoami)",
        "echo `whoami`",
        "diff <(ls) <(ls -a)",
        "tee >(wc -l)",
        "echo a\necho b",
        "sleep 1 &",
    ):
        assert guard(command, rules) is True, f"compound must pass through: {command!r}"


def test_decision_simple_command_matching_rule_passes_through() -> None:
    guard = _pretool_mod._deny_guard_passthrough
    assert guard("printf 'X\\n'", ["printf:*"]) is True
    assert guard("touch f.txt", ["touch:*"]) is True
    assert guard("curl x", ["curl:*"]) is True


def test_decision_simple_command_matching_no_rule_rewrites() -> None:
    guard = _pretool_mod._deny_guard_passthrough
    assert guard("cat bigfile", ["printf:*", "touch:*"]) is False


def test_decision_obscured_or_unparseable_passes_through() -> None:
    """Env-assignment prefixes hide the real verb; shlex failures mean we cannot
    name the verb at all. Both are doubt → passthrough (when any rule exists)."""
    guard = _pretool_mod._deny_guard_passthrough
    rules = ["printf:*"]
    assert guard("FOO=1 printf x", rules) is True
    assert guard('printf "unclosed', rules) is True
    assert guard("''", rules) is True  # no discernible verb


# --- acceptance: the real hook subprocess against settings on disk (G3) -----------


def _run_hook(
    command: str,
    tmp: Path,
    *,
    cwd_settings: dict | str | None = None,
    cwd_local_settings: dict | None = None,
    home_settings: dict | None = None,
    project_dir_settings: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run pretool_pipe.py hermetically: HOME, the payload cwd, and (when given)
    CLAUDE_PROJECT_DIR are fresh directories carrying exactly the settings each
    scenario specifies."""
    proj = tmp / "proj"
    home = tmp / "home"
    proj.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    if cwd_settings is not None:
        target = proj / ".claude" / "settings.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        text = cwd_settings if isinstance(cwd_settings, str) else json.dumps(cwd_settings)
        target.write_text(text, encoding="utf-8")
    if cwd_local_settings is not None:
        _write_settings(proj / ".claude" / "settings.local.json", cwd_local_settings)
    if home_settings is not None:
        _write_settings(home / ".claude" / "settings.json", home_settings)
    env = {"HOME": str(home), "FURL_PRETOOL_PIPE": "1", "PATH": "/usr/bin:/bin"}
    if project_dir_settings is not None:
        project_root = tmp / "project-root"
        _write_settings(project_root / ".claude" / "settings.json", project_dir_settings)
        env["CLAUDE_PROJECT_DIR"] = str(project_root)
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(proj)}
    return subprocess.run(
        [sys.executable, str(_PRETOOL)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def _deny(*rules: str) -> dict:
    return {"permissions": {"deny": list(rules)}}


def _ask(*rules: str) -> dict:
    return {"permissions": {"ask": list(rules)}}


def test_denied_printf_passes_through(tmp_path) -> None:
    """G3 scenario 1 — THE reviewer-84 F3 reproducer: deny ``Bash(printf:*)``
    plus ``printf 'X\\n'`` must emit NOTHING so the deterministic deny fires on
    the original. Pre-guard the hook rewrote here (deny downgraded to ask)."""
    proc = _run_hook("printf 'X\\n'", tmp_path, cwd_settings=_deny("Bash(printf:*)"))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", "a denied command must never be rewritten"


def test_denied_touch_passes_through(tmp_path) -> None:
    """G3 scenario 2."""
    proc = _run_hook("touch f.txt", tmp_path, cwd_settings=_deny("Bash(touch:*)"))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_asked_curl_passes_through(tmp_path) -> None:
    """G3 scenario 3: ask rules gate exactly like deny rules — the ask prompt
    must still fire on the ORIGINAL command."""
    proc = _run_hook("curl x", tmp_path, cwd_settings=_ask("Bash(curl:*)"))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_no_rules_rewrites_allowed_command(tmp_path) -> None:
    """G3 scenario 4: with NO deny/ask Bash rules the pipe rewrites normally —
    zero-config savings preserved (allow rules do not gate)."""
    settings = {"permissions": {"allow": ["Bash(cat:*)"], "deny": [], "ask": []}}
    proc = _run_hook("cat bigfile", tmp_path, cwd_settings=settings)
    assert proc.returncode == 0, proc.stderr
    assert "updatedInput" in proc.stdout, "zero deny/ask rules must still rewrite"


def test_compound_with_deny_rule_passes_through(tmp_path) -> None:
    """G3 scenario 5: a compound command is never parsed segment-by-segment —
    with any deny/ask rule present it passes through wholesale."""
    proc = _run_hook("printf x | tee y", tmp_path, cwd_settings=_deny("Bash(printf:*)"))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_malformed_settings_passes_through(tmp_path) -> None:
    """G3 scenario 6: unreadable rules = unknowable rules → fail-safe, even for
    an innocuous command."""
    proc = _run_hook("cat bigfile", tmp_path, cwd_settings="{definitely not json")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_blanket_bash_deny_passes_through_everything(tmp_path) -> None:
    """A bare ``Bash`` deny/ask rule bounds the hook's CLI/policy blindness:
    every Bash command passes through."""
    proc = _run_hook("cat bigfile", tmp_path, cwd_settings=_deny("Bash"))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_home_settings_rules_apply(tmp_path) -> None:
    """User-scope ``~/.claude/settings.json`` gates exactly like project scope."""
    proc = _run_hook("printf hi", tmp_path, home_settings=_deny("Bash(printf:*)"))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_claude_project_dir_scope_rules_apply(tmp_path) -> None:
    """Claude Code loads project settings from the session's PROJECT ROOT
    (``CLAUDE_PROJECT_DIR``, which every hook receives), while the payload cwd
    can be a subdirectory with no ``.claude`` of its own. A deny rule at the
    project root must still gate the pipe."""
    proc = _run_hook("printf hi", tmp_path, project_dir_settings=_deny("Bash(printf:*)"))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", "project-root (CLAUDE_PROJECT_DIR) deny rules must gate"


def test_cwd_local_settings_rules_apply(tmp_path) -> None:
    """Project-local ``.claude/settings.local.json`` gates too."""
    proc = _run_hook("curl x", tmp_path, cwd_local_settings=_ask("Bash(curl:*)"))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_unrelated_deny_rule_still_rewrites_simple_command(tmp_path) -> None:
    """Savings are only skipped where a rule could apply: a simple command whose
    verb matches no deny/ask rule still rewrites."""
    proc = _run_hook("cat bigfile", tmp_path, cwd_settings=_deny("Bash(rm:*)"))
    assert proc.returncode == 0, proc.stderr
    assert "updatedInput" in proc.stdout


def test_env_assignment_prefix_passes_through_when_rules_exist(tmp_path) -> None:
    """``FOO=1 printf x`` hides the verb behind an assignment — with a printf
    deny present it must pass through, not be misread as verb ``FOO=1``."""
    proc = _run_hook("FOO=1 printf x", tmp_path, cwd_settings=_deny("Bash(printf:*)"))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_guard_is_hermetic_in_this_suite() -> None:
    """Meta-pin: every acceptance test above supplies its own HOME, so a
    developer's real ~/.claude rules can never decide these outcomes."""
    with tempfile.TemporaryDirectory() as tmp:
        proc = _run_hook("cat bigfile", Path(tmp))
        assert "updatedInput" in proc.stdout
