#!/usr/bin/env python3
"""Furl PreToolUse hook (ON BY DEFAULT — disable with FURL_PRETOOL_PIPE=0): the
real-savings compression path that does NOT depend on PostToolUse
``updatedToolOutput`` being applied at all. Claude Code >=2.1.163, the floor
tracked as ``MIN_VERSION_FOR_POST_TOOL_USE_REPLACEMENT`` in
``furl_ctx/host_version.py``, schema-validates that field and APPLIES a
correctly shape-mirrored replacement, dropping only ones that fail
validation; see ``compress_tool_output.py`` and anthropics/claude-code#68951.
This pipe's justification is independent of that fact: it banks savings
before the tool result even exists rather than after, it is the only
compression path active on hosts below that version floor, and it covers
output shapes ``compress_tool_output.py``'s mirroring cannot reproduce.

Unless disabled, this rewrites a ``Bash`` command so its STDOUT is piped through
the Furl compressor (``pipe_compress.py``) BEFORE it becomes the tool result —
so the model-visible output IS the compressed form, with the original stored
under a ``<<ccr:HASH>>`` marker (retrievable via ``furl_retrieve``), exactly
like the PostToolUse path.

SMART DEFAULT (v10, user-approved): the pipe runs UNLESS ``FURL_PRETOOL_PIPE``
is EXPLICITLY falsy — ``0``/``false``/``off``/``no``/``disabled``
(case-insensitive, ALL whitespace removed). Unset, empty, and any other value —
including unknown junk — leave it ON ("on unless explicitly disabled", so a typo
never silently disables savings). Only ``Bash`` is touched.

CORE PROPERTY — PERMISSION-RULE SAFETY (provably-safe redesign, non-negotiable,
total): this hook MUST NEVER rewrite a command that Claude Code would subject to
a permissions **deny** or **ask** rule. Claude Code evaluates those rules against
the REWRITTEN command, and the furl-pipe wrapper no longer matches
``Bash(verb:*)`` patterns — so rewriting a governed command would silently
downgrade a hard deny to "ask" (normal mode) or trip the obfuscation classifier
(auto mode). The invariant is made TOTAL by a single predicate instead of
fragile per-verb matching: the hook rewrites a Bash command ONLY IF there are
ZERO readable Bash permission rules. If ANY Bash rule of ANY kind exists — deny,
ask, OR allow (see below), blanket or scoped — across the scopes CC actually
uses: enterprise managed settings (the per-OS ``managed-settings.json`` + its
``managed-settings.d`` fragments, OR the ``$CLAUDE_CODE_MANAGED_SETTINGS_PATH``
override), project scope (``.claude/settings{,.local}.json`` under BOTH
``$CLAUDE_PROJECT_DIR`` and the payload cwd), or user scope
(``settings{,.local}.json`` under BOTH ``~/.claude`` AND ``$CLAUDE_CONFIG_DIR``),
it PASSES THROUGH ALL Bash: no rewrite, no per-verb analysis. This makes "never
mask a deny/ask rule" TOTAL — no command shape (a command-modifier wrapper CC
sees through like ``env``/``sudo``/``flock``/``strace``, a compound, or anything
CC's closed-source resolver interprets) can be masked, because when a rule exists
NOTHING is rewritten. ANY doubt (unreadable or malformed settings, or a
config-path override env var that is set but cannot be confidently resolved) also
PASSES THROUGH. Fail toward no-compression, never toward masking a permission
rule; no-savings is acceptable, defeating a rule is not. WHY ``allow`` COUNTS: an
allow-list config makes UNLISTED commands restricted (ask/deny by default), so
the mere presence of allow rules is itself a maskable restrictive posture —
including it keeps the invariant total and is maximally conservative, while still
meeting the zero-config criterion (a fresh install has NO permissions config →
rewrite → savings). HONEST BLINDNESS (the genuine residual): the hook still
cannot see CLI flags (``--permission-mode``, ``--disallowedTools``), SDK
``managedSettings`` options, or API-fetched remote org policy
(``$CLAUDE_CODE_REMOTE_SETTINGS_PATH`` / ``remoteSettings`` — a session-scope
fetch, not a normal file). Users relying on those for Bash restrictions should
set ``FURL_PRETOOL_PIPE=0`` (documented in the plugin README).

CONSCIOUS OVERRIDE (``FURL_PIPE_WITH_RULES``): the zero-rules invariant costs the
savings for the many users who keep even one Bash permission rule — the pipe then
does nothing on every Bash call. A safe subset that keeps it on WITH rules present
was investigated and rejected: the rewrite wraps the command in a comment, a
``$(mktemp)`` substitution, a subshell, and a ``uv run`` compressor pipe, and Claude
Code matches permission patterns against that REWRITTEN text, not the original. For
ANY rule kind — allow, deny, or ask — the rewrite can change which rule matches: the
governed verb is buried inside a subshell, a new ``python3``/``uv`` sub-command
appears, and the substitution/pipe shape can trip the auto-mode obfuscation
classifier. Proving match-invariance would mean reimplementing Claude Code's
closed-source, version-dependent resolver exactly, so NO subset is provably safe and
the total invariant stands. A user who has read this trade-off and accepts that the
rewrite may change how their Bash rules match may set ``FURL_PIPE_WITH_RULES`` to an
explicit truthy value (1/true/on/yes/enabled) to rewrite anyway; it is OFF unless
explicitly set, so a typo never enables it, and it bypasses BOTH the rule gate and
the settings-doubt gate.

OBSERVABILITY (the gating decision was invisible before): every passthrough now
records WHY into the shared per-project CCR store via ``_furl_ccr_counters`` — a
``pipe_noop:<reason>`` counter (``permission-rules``, ``settings-doubt``,
``disabled-env``, ``not-bash``, ``already-wrapped``, and the malformed-input
buckets) — so ``furl_stats`` shows a rules-gated pipe instead of silent nothing, and
the SessionStart banner reports the same live status from the same detection. The
counter write is best-effort and fail-open, never changes the hook's stdout or exit,
and is resolved lazily so the rewrite path adds no furl_ctx import.

Contract (PreToolUse):
  stdin  : JSON {tool_name, tool_input:{command, ...}, cwd, ...}
  stdout : to REWRITE, emit {"hookSpecificOutput": {"hookEventName":
           "PreToolUse", "updatedInput": {...tool_input, "command": <rewritten>}}}
  stdout empty + exit 0 : the original command runs unchanged.

The rewrite preserves the original command's EXIT CODE exactly. STDERR is never
captured and flows live — but because stdout is buffered for compression,
stderr/stdout interleaving is not preserved: in a merged view all stderr appears
before the (possibly compressed) stdout; ``cmd 2>&1`` merges both into the
compressed stream. Small outputs pass through raw (the compressor's own
threshold). FAIL-OPEN at the shell level twice over: a compressor that cannot
even start falls back to ``cat`` of the captured output, and if the stdout
tempfile cannot even be created the original command runs UNWRAPPED
(uncompressed, uncounted) — never a broken command.
FAIL-OPEN here too: any error emits nothing (exit 0) → original command runs.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, NamedTuple

# The engine pin MUST match hooks.json's command pins (a test asserts this) so
# the compressor resolves the SAME furl-ctx the rest of the plugin uses.
# Supply-chain posture: version-pinned, NOT hash-pinned, and `uv run --no-project`
# bypasses any lockfile so transitive deps float (audit High-6). The honest
# posture and the hardening path (pre-install a hash-verified engine on PATH) are
# documented in SECURITY.md → "Supply chain".
_FURL_CTX_PIN = "furl-ctx[mcp]==1.3.2"

# Transparency marker: prepended to the rewritten command (visible in the
# transcript). Names the OPT-OUT since the pipe is on by default.
_PIPE_MARKER = "# furl-pipe (FURL_PRETOOL_PIPE=0 to disable)"

# Loop guard: the STABLE PREFIX of every marker version this plugin has ever
# emitted (old opt-in markers said "(FURL_PRETOOL_PIPE=1)"), so a command
# wrapped by ANY plugin version is never double-wrapped after an upgrade.
_PIPE_GUARD = "# furl-pipe"

_ENABLE_ENV = "FURL_PRETOOL_PIPE"

# The explicit opt-out set (S1 smart default). Shared semantics with the
# hooks.json shell gate — a parity test enumerates both.
_DISABLE_VALUES = frozenset({"0", "false", "off", "no", "disabled"})

# ASCII whitespace removal — the exact character class the shell gate's
# ``tr -d "[:space:]"`` deletes (POSIX locale), so both gates normalize
# identically even for values with INTERNAL whitespace (review-84 F1).
_WS_REMOVE = str.maketrans("", "", " \t\n\r\f\v")

# CONSCIOUS OVERRIDE (opt-IN, OFF unless explicitly set). A truthy value rewrites
# Bash EVEN WHEN permission rules exist. No rule-present subset is provably safe
# (see the module docstring), so this is a documented conscious trade-off, never a
# default. Truthy-only so a typo cannot silently enable the risky path; the truthy
# set mirrors the plugin's other truthy flags (compress_tool_output._flag_enabled).
_WITH_RULES_ENV = "FURL_PIPE_WITH_RULES"
_WITH_RULES_TRUE_VALUES = frozenset({"1", "true", "on", "yes", "enabled"})

# Gating no-op reasons, recorded into furl_stats as ``pipe_noop:<reason>`` (via the
# shared ``PIPE_NOOP_PREFIX``). Named constants, not scattered string literals, so
# the tests and the banner reference one source of truth.
_NOOP_DISABLED_ENV = "disabled-env"
_NOOP_BAD_STDIN = "bad-stdin"
_NOOP_EMPTY_STDIN = "empty-stdin"
_NOOP_BAD_JSON = "bad-json"
_NOOP_NON_DICT_PAYLOAD = "non-dict-payload"
_NOOP_NOT_BASH = "not-bash"
_NOOP_BAD_TOOL_INPUT = "bad-tool-input"
_NOOP_NO_COMMAND = "no-command"
_NOOP_ALREADY_WRAPPED = "already-wrapped"
_NOOP_PERMISSION_RULES = "permission-rules"
_NOOP_SETTINGS_DOUBT = "settings-doubt"
_NOOP_EMIT_ERROR = "emit-error"


def _pipe_disabled(raw: str | None) -> bool:
    """SMART DEFAULT (v10, user-approved): the pipe runs UNLESS explicitly
    disabled. True only for an explicit falsy value — 0/false/off/no/disabled,
    case-insensitive with ALL ASCII whitespace removed (so `` o f f `` is OFF,
    matching the shell gate's ``tr -d "[:space:]"`` exactly). Unset (None),
    empty, and any unrecognized value return False (pipe ON): "on unless
    explicitly disabled", so a typo like ``FURL_PRETOOL_PIPE=fasle`` never
    silently disables savings. SEMANTICALLY IDENTICAL to the hooks.json shell
    gate for every value (test_pretool_gate_parity_shell_and_python enumerates
    both, internal-whitespace cases included)."""
    if raw is None:
        return False
    return raw.translate(_WS_REMOVE).lower() in _DISABLE_VALUES


def _pipe_with_rules_override(raw: str | None) -> bool:
    """``FURL_PIPE_WITH_RULES``: True ONLY for an explicit truthy value
    (1/true/on/yes/enabled, case-insensitive, ALL ASCII whitespace removed). Unset,
    empty, falsy, and unknown values return False — this is an OPT-IN (off unless
    explicitly set), the inverse posture of ``_pipe_disabled``, so a typo never
    enables the risky rewrite-with-rules path. When True the pipe rewrites Bash even
    though permission rules exist or settings are unreadable; the user has accepted
    the trade-off in the module docstring that the rewrite may change how Claude
    Code matches their Bash permission rules."""
    if raw is None:
        return False
    return raw.translate(_WS_REMOVE).lower() in _WITH_RULES_TRUE_VALUES


# --- gating observability (best-effort, fail-open) --------------------------------
# Every passthrough records WHY into the shared per-project CCR store so ``furl_stats``
# surfaces a rules-gated pipe instead of the silent nothing the report flagged (F3).
# The store is resolved LAZILY on the first record, so the rewrite/savings path never
# imports furl_ctx: the ~0.1s furl_ctx import behind ``resolve_store`` is paid only
# when the pipe PASSES a command through, never when it rewrites one. In production a
# falsy ``FURL_PRETOOL_PIPE`` is filtered by the hooks.json shell gate before this
# script runs, so a disabled pipe pays nothing here either.
_counter_ctx_cache: tuple[Any, Any] | None = None
_counter_ctx_resolved = False


def _counters_module() -> Any:
    """Lazily import the sibling counter helper; None when unavailable (fail-open),
    so counting degrades to a no-op instead of ever raising into the hook."""
    try:
        import _furl_ccr_counters

        return _furl_ccr_counters
    except Exception:
        return None


def _counter_ctx() -> tuple[Any, Any]:
    """``(counters_module, store)`` resolved once per process, memoized. Pins the
    durable sqlite backend and the per-project namespace (``CLAUDE_PROJECT_DIR`` —
    the standard hook env — else the cwd) so the tally lands in the SAME store
    ``furl_stats`` reads, identical namespacing to compress_tool_output.py and
    pipe_compress.py. ``setdefault`` keeps any user override intact."""
    global _counter_ctx_cache, _counter_ctx_resolved
    if not _counter_ctx_resolved:
        _counter_ctx_resolved = True
        cmod = _counters_module()
        store = None
        if cmod is not None:
            os.environ.setdefault("FURL_CCR_BACKEND", "sqlite")
            os.environ.setdefault(
                "FURL_CCR_PROJECT_DIR",
                os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(),
            )
            store = cmod.resolve_store()
        _counter_ctx_cache = (cmod, store)
    return _counter_ctx_cache


def _record_noop(reason: str | None) -> None:
    """Tally one ``pipe_noop:<reason>`` bucket, best-effort. A counter problem may
    never break the hook — a crash here would block the user's Bash — but it is NOT
    swallowed silently: an unexpected failure leaves ONE stderr breadcrumb (surfaced
    to the user, never to the model) and continues. ``bump``/``resolve_store`` are
    themselves fail-open, so this except is defensive belt-and-suspenders."""
    if not reason:
        return
    try:
        cmod, store = _counter_ctx()
        if cmod is not None and store is not None:
            cmod.bump(store, cmod.PIPE_NOOP_PREFIX + reason)
    except Exception as exc:
        try:
            sys.stderr.write(
                f"furl-pipe: could not record pipe_noop:{reason} [{exc.__class__.__name__}]\n"
            )
        except Exception:
            pass


def _passthrough(reason: str | None = None) -> None:
    """Emit nothing and succeed: the original command runs unchanged. Records the
    gating *reason* (why the pipe declined this command) so ``furl_stats`` is no
    longer left guessing — the whole point of the report's F3 fix."""
    _record_noop(reason)
    sys.exit(0)


# --- permission-rule guard (provably-safe redesign) -------------------------------
# The TOTAL invariant: rewrite a Bash command ONLY when ZERO readable Bash
# permission rules exist. No per-verb matching, no wrapper denylist, no compound
# analysis — existence of ANY rule is all we check, because when a rule exists
# NOTHING is rewritten, so no command shape can mask it. See the module docstring.

# Enterprise managed-settings.json DEFAULT locations, per OS. Paths VERIFIED
# against code.claude.com/docs/en/settings (reviewer-guard G2) — never guessed.
# An unrecognized platform maps to nothing (we do not invent a path). WSL shares
# the Linux path. The location can be relocated via $CLAUDE_CODE_MANAGED_SETTINGS_PATH
# (honored in ``_managed_settings_paths``), so a Bash rule that lives only in
# managed settings — wherever they are — must still gate the pipe.
_MANAGED_BASE_BY_PLATFORM = {
    "darwin": Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
    "linux": Path("/etc/claude-code/managed-settings.json"),
    "win32": Path(r"C:\Program Files\ClaudeCode\managed-settings.json"),
}


def _managed_dir_paths(directory: Path, filename: str = "managed-settings.json") -> list[Path]:
    """``<directory>/<filename>`` plus any ``<directory>/managed-settings.d/*.json``
    drop-in fragments (Claude Code merges those alphabetically). A drop-in
    directory that cannot be enumerated is returned AS its path, which reads as
    ``OSError`` in the loader → doubt → passthrough (never a silently missed
    policy)."""
    base = directory / filename
    dropin = directory / "managed-settings.d"
    try:
        fragments = sorted(dropin.glob("*.json")) if dropin.is_dir() else []
    except OSError:
        return [base, dropin]
    return [base, *fragments]


def _managed_settings_paths() -> tuple[list[Path], bool]:
    """``(paths, doubt)`` for enterprise managed settings.

    Honors ``$CLAUDE_CODE_MANAGED_SETTINGS_PATH`` when set. Its file-vs-directory
    semantics are not pinned in public docs, so rather than GUESS one we INSPECT
    the path at runtime — a FILE is read directly; a DIRECTORY is read as
    ``managed-settings.json`` + its ``managed-settings.d`` fragments. If the
    override is set but resolves to NEITHER a readable file nor a directory
    (missing/broken/permission-errored), that is DOUBT → passthrough: a
    set-but-unresolvable managed policy must never be silently ignored and let a
    rewrite through. When the override is unset, fall back to the verified per-OS
    default location (nothing on an unrecognized platform)."""
    override = os.environ.get("CLAUDE_CODE_MANAGED_SETTINGS_PATH", "").strip()
    if override:
        target = Path(override)
        try:
            if target.is_file():
                return [target], False
            if target.is_dir():
                return _managed_dir_paths(target), False
        except OSError:
            return [], True
        return [], True  # set but neither a readable file nor a directory → doubt
    base = _MANAGED_BASE_BY_PLATFORM.get(sys.platform)
    if base is None:
        return [], False
    return _managed_dir_paths(base.parent, base.name), False


# Human-readable scope labels for the SessionStart banner's OFF message and
# ``_scan_bash_rules``. Kept short and free of round brackets / dashes because the
# banner systemMessage they end up in is pinned AI-tell-free (test_hook_audit_fixes).
_SCOPE_MANAGED = "enterprise managed settings"
_SCOPE_PROJECT = "project settings"
_SCOPE_USER = "user settings"


def _settings_path_groups(cwd: str) -> tuple[tuple[tuple[str, tuple[Path, ...]], ...], bool]:
    """``(scope_groups, managed_doubt)`` — permission-rule sources grouped BY the
    scope Claude Code loads them from, so a caller can report WHICH scope carried a
    rule. ``_settings_paths`` flattens this; ``_scan_bash_rules`` keeps the labels.
    ONE shared path-discovery implementation, so the pipe gate and the banner cannot
    drift on what they read.

    Scopes across everything Claude Code ACTUALLY uses (relocations included):

    * enterprise managed settings (``_managed_settings_paths`` — per-OS default or
      the ``$CLAUDE_CODE_MANAGED_SETTINGS_PATH`` override; the only ``doubt`` source);
    * project scope from BOTH ``$CLAUDE_PROJECT_DIR`` (the session's project root,
      where CC loads project settings) AND the payload cwd — usually the same, but
      when they differ the union can only add rules, never mask one;
    * user scope from BOTH ``~/.claude`` AND (when set) ``$CLAUDE_CONFIG_DIR``. CC
      uses ``${CLAUDE_CONFIG_DIR:-$HOME/.claude}`` (the env var REPLACES the home
      dir); reading BOTH strictly dominates that, so a relocated user rule can
      never be missed. Over-reading is always safe (more rules → more passthrough).

    Genuinely invisible here (the documented residual — set ``FURL_PRETOOL_PIPE=0``
    if you restrict Bash only through these): CLI ``--permission-mode`` /
    ``--disallowedTools`` flags, SDK ``managedSettings`` options, and API-fetched
    remote org policy (``$CLAUDE_CODE_REMOTE_SETTINGS_PATH`` / ``remoteSettings``,
    a session-scope fetch, not a normal file). ``~/.claude.json`` is intentionally
    NOT read: it carries no deny/ask rules (only ``allowedTools``), so a missed
    entry there is a savings nit, not a bypass."""
    project_dirs: list[Path] = []
    project_root = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if project_root:
        project_dirs.append(Path(project_root))
    if Path(cwd) not in project_dirs:
        project_dirs.append(Path(cwd))
    project_scopes = [d / ".claude" for d in project_dirs]
    # User scope: the UNION of ~/.claude and $CLAUDE_CONFIG_DIR (deduped).
    user_scopes: list[Path] = [Path.home() / ".claude"]
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if config_dir and Path(config_dir) not in user_scopes:
        user_scopes.append(Path(config_dir))

    def _scoped(dirs: list[Path]) -> tuple[Path, ...]:
        return tuple(
            scope / name for scope in dirs for name in ("settings.json", "settings.local.json")
        )

    managed_paths, managed_doubt = _managed_settings_paths()
    groups: tuple[tuple[str, tuple[Path, ...]], ...] = (
        (_SCOPE_MANAGED, tuple(managed_paths)),
        (_SCOPE_PROJECT, _scoped(project_scopes)),
        (_SCOPE_USER, _scoped(user_scopes)),
    )
    return groups, managed_doubt


def _settings_paths(cwd: str) -> tuple[tuple[Path, ...], bool]:
    """``(paths, doubt)`` — flat view of every permission-rule source, preserving
    the historical order (managed, then project, then user). A thin flatten over
    ``_settings_path_groups`` so there is exactly ONE path-discovery implementation
    behind both this and ``_scan_bash_rules``."""
    groups, managed_doubt = _settings_path_groups(cwd)
    flat = tuple(path for _label, paths in groups for path in paths)
    return flat, managed_doubt


def _bash_bodies_from_entries(entries: object) -> tuple[list[str | None], bool]:
    """Collect the Bash-governing rule bodies from one deny/ask/allow array.

    Returns ``(bodies, doubt)``: any Bash entry contributes a body (``None`` for
    a BLANKET bare ``Bash`` or an unparseable ``Bash(...``; the string body
    otherwise). Only EXISTENCE matters downstream, so the body content is now
    incidental — but it stays a faithful record of what was found. Rules for
    other tools (including ``BashOutput``, which merely shares the prefix) are
    irrelevant to a Bash rewrite and are skipped. Any shape we cannot read raises
    *doubt* instead of being guessed at."""
    bodies: list[str | None] = []
    if not isinstance(entries, list):
        return bodies, True
    doubt = False
    for entry in entries:
        if not isinstance(entry, str):
            doubt = True
            continue
        rule = entry.strip()
        # A recognizable Bash rule is EXACTLY ``Bash`` or starts with ``Bash(``.
        # The open paren disambiguates ``Bash(...)`` from the sibling tool
        # ``BashOutput`` (which does not govern command execution). Whitespace is
        # stripped first, so `` Bash(rm:*) `` and ``Bash( rm:*)`` both count. An
        # UNTERMINATED but recognizable rule (``Bash(rm:*`` — no close paren)
        # still counts as PRESENT (a ``None`` body) and is never silently
        # dropped: presence is what gates the rewrite, so a rule we can see but
        # not fully parse must still force passthrough.
        if rule == "Bash":
            bodies.append(None)
        elif rule.startswith("Bash("):
            bodies.append(rule[5:-1] if rule.endswith(")") else None)
    return bodies, doubt


def _load_bash_rule_bodies(paths: tuple[Path, ...]) -> tuple[list[str | None], bool]:
    """Union of every ``deny``/``ask``/``allow`` Bash rule body across *paths*.

    Precedence is irrelevant to a conservative union: a rule in ANY scope could
    gate the command, so all of them count. ``allow`` is included because an
    allow-list config makes UNLISTED commands restricted, so its presence is
    itself a maskable posture (see the module docstring). ``doubt`` is True when
    a source EXISTS but cannot be read or parsed (unreadable file, invalid JSON,
    wrong shapes) — the caller must pass through, because unknowable rules could
    contain a deny. A missing file is not doubt; it simply has no rules."""
    bodies: list[str | None] = []
    doubt = False
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            doubt = True
            continue
        try:
            data = json.loads(text)
        except ValueError:
            doubt = True
            continue
        if not isinstance(data, dict):
            doubt = True
            continue
        permissions = data.get("permissions")
        if permissions is None:
            continue
        if not isinstance(permissions, dict):
            doubt = True
            continue
        for key in ("deny", "ask", "allow"):
            if key not in permissions:
                continue
            found, entry_doubt = _bash_bodies_from_entries(permissions[key])
            bodies.extend(found)
            doubt = doubt or entry_doubt
    return bodies, doubt


def _has_any_bash_rule(bodies: list[str | None]) -> bool:
    """The TOTAL invariant's predicate: True iff ANY readable Bash permission
    rule exists (deny/ask/allow, blanket or scoped). A rewrite happens ONLY when
    this is False (and there is no doubt) — no per-verb matching, because when a
    rule exists NOTHING is rewritten, so no command shape can mask it.

    Uses ``bool(bodies)`` (list non-empty), NOT ``any(bodies)``: a blanket or
    unterminated rule contributes a ``None`` body, and ``any([None])`` is False
    while ``bool([None])`` is True — a recognizable rule must count as present
    even when its body could not be parsed."""
    return bool(bodies)


class BashRuleScan(NamedTuple):
    """Result of scanning every readable settings scope for Bash permission rules.

    * ``rule_count`` — total readable Bash rules found (deny/ask/allow, any scope).
    * ``scopes`` — human labels of the scopes that carried at least one rule.
    * ``doubt`` — a source EXISTS but could not be read or parsed, so the true rule
      set is unknowable.

    Either ``rule_count > 0`` or ``doubt`` means the pipe must NOT rewrite (unless
    the conscious ``FURL_PIPE_WITH_RULES`` override is set). This is the exact signal
    both the pipe gate and the SessionStart banner consume, so what the banner
    reports is precisely what the pipe enforces."""

    rule_count: int
    scopes: tuple[str, ...]
    doubt: bool


def _scan_bash_rules(cwd: str) -> BashRuleScan:
    """Scan every settings scope for Bash permission rules, keeping per-scope
    attribution. The single detection the pipe gate (existence + doubt) and the
    banner (count + scopes) both call, built on the same ``_settings_path_groups``
    discovery and ``_load_bash_rule_bodies`` reader so the two cannot drift.

    Equivalent to the flat ``_settings_paths`` + ``_load_bash_rule_bodies`` +
    ``_has_any_bash_rule`` pipeline the gate used before: ``rule_count > 0`` iff
    ``_has_any_bash_rule`` was True, and ``doubt`` is the same OR of path and load
    doubt — just grouped so the count and scope attribution survive for the banner."""
    groups, doubt = _settings_path_groups(cwd)
    total = 0
    scopes_with_rules: list[str] = []
    for label, paths in groups:
        bodies, load_doubt = _load_bash_rule_bodies(paths)
        doubt = doubt or load_doubt
        if _has_any_bash_rule(bodies):
            total += len(bodies)
            scopes_with_rules.append(label)
    return BashRuleScan(rule_count=total, scopes=tuple(scopes_with_rules), doubt=doubt)


def _rewrite_command(original: str, project_dir: str, compressor: str) -> str:
    """Build the exit-code-preserving, fail-open pipe rewrite of *original*.

    Design:
      * ``if [ -n "$f" ] && : >"$f"`` — review F1 guard: PROBE that the tempfile
        is actually creatable/writable BEFORE any capture redirect touches the
        original. If the probe fails (mktemp unavailable, so the substitution is
        empty, or the mktemp file is unwritable), the ``else`` branch runs the ORIGINAL COMMAND
        with no redirect — inside a SUBSHELL whose ``)`` sits on its own line
        (review R1), exactly like the then branch: a bare interpolation would let
        an original ending in an ODD number of trailing backslashes line-continue
        into ``fi``, making the WHOLE script a parse error (rc 2, command never
        runs in either branch). The subshell is the branch's last statement, so
        stdout and the exit code still flow through exactly (fail-open: no
        compression rather than no command). Pre-F1, the redirect sat unprobed on
        the subshell and a tempfile failure meant the command NEVER RAN.
      * ``( <orig>\\n) >"$f"`` — a SUBSHELL captures only stdout to the tempfile;
        the closing ``)`` on its own line survives an *orig* that ends in a
        comment/``&``/heredoc. STDERR is never redirected — it flows live — but
        since stdout is buffered here and emitted at the end, stderr/stdout
        interleaving is NOT preserved: merged views show all stderr before the
        (possibly compressed) stdout; ``2>&1`` merges into the compressed stream.
      * an INT/TERM ``trap`` set before the capture and cleared right after it
        (F-A2): because stdout is buffered, a command KILLED mid-run by Claude
        Code's timeout or a ``run_in_background`` kill would otherwise lose ALL
        its output; the trap flushes whatever was captured so the partial stdout
        still reaches the transcript, then re-raises the signal for a faithful
        128+signal exit. Cleared after capture so the compressor path owns its
        own stdout.
      * ``__furl_ec=$?`` right after captures the original's exact exit code
        (the subshell's = its last command's), restored by the final ``exit``.
      * the compressor reads the tempfile; ``|| cat "$f"`` is the shell-level
        fail-open — if the compressor cannot even start (no ``uv``/python), the
        RAW captured output is emitted, never lost.
      * ``FURL_CCR_PROJECT_DIR`` + ``FURL_CCR_BACKEND=sqlite`` are baked so the
        compressor writes the original into the SAME durable per-project store
        the MCP server reads (a memory store would make the marker unretrievable).
        The per-namespace spill tier (T6) is enabled inside pipe_compress.py via
        ``setdefault("FURL_CCR_SPILL", "1")``, which honors a user opt-out, so it
        is deliberately NOT forced here.
    """
    qdir = shlex.quote(project_dir)
    qcomp = shlex.quote(compressor)
    return (
        f"{_PIPE_MARKER}\n"
        # F-A3: mktemp ONLY. mktemp makes a unique 0600 file atomically, so no
        # predictable-name symlink race exists; if mktemp is unavailable the
        # substitution is empty and the guard below runs the command UNWRAPPED
        # (clean fail-open), so the 0600 posture holds on every path.
        "__furl_f=$(mktemp 2>/dev/null || mktemp -t furlpipe 2>/dev/null)\n"
        # NOTE: ``2>/dev/null`` BEFORE ``>"$f"`` — redirections process left to
        # right, so suppression must be in place before the probe redirect can
        # fail, or the failure message would leak to the live stderr stream.
        'if [ -n "$__furl_f" ] && : 2>/dev/null >"$__furl_f"; then\n'
        # F-A2: on a fatal signal (Claude Code timeout / run_in_background kill)
        # flush the captured partial stdout so it still reaches the transcript
        # (bare bash streams live; this capture buffers), remove the tempfile,
        # then die by the same signal for a faithful 128+signal status. Cleared
        # right after capture so the compressor path owns its own stdout.
        'trap \'cat "$__furl_f" 2>/dev/null; rm -f "$__furl_f" 2>/dev/null; trap - INT; kill -INT $$\' INT\n'
        'trap \'cat "$__furl_f" 2>/dev/null; rm -f "$__furl_f" 2>/dev/null; trap - TERM; kill -TERM $$\' TERM\n'
        f"( {original}\n"
        ') >"$__furl_f"\n'
        "__furl_ec=$?\n"
        "trap - INT TERM\n"
        f"FURL_CCR_PROJECT_DIR={qdir} FURL_CCR_BACKEND=sqlite "
        f'uv run --no-project --with "{_FURL_CTX_PIN}" python3 {qcomp} <"$__furl_f"'
        ' || cat "$__furl_f"\n'
        'rm -f "$__furl_f"\n'
        "exit $__furl_ec\n"
        "else\n"
        'rm -f "$__furl_f" 2>/dev/null\n'
        f"( {original}\n"
        ")\n"
        "fi"
    )


def _project_dir(payload: dict) -> str:
    """Resolve the project dir the SAME way the PostToolUse hook and MCP server
    do (CLAUDE_PROJECT_DIR -> payload cwd -> getcwd), so the pipe's CCR writes
    land in the store the MCP server reads."""
    cwd = payload.get("cwd")
    return (
        os.environ.get("CLAUDE_PROJECT_DIR")
        or (cwd if isinstance(cwd, str) and cwd.strip() else "")
        or os.getcwd()
    )


def main() -> None:
    # Opt-OUT gate FIRST (S1 smart default). In production the hooks.json shell gate
    # already skips a falsy FURL_PRETOOL_PIPE before this script is even spawned, so
    # this python mirror is defense-in-depth; when it IS reached (direct invocation)
    # it records the reason like every other passthrough.
    if _pipe_disabled(os.environ.get(_ENABLE_ENV)):
        _passthrough(_NOOP_DISABLED_ENV)

    try:
        raw = sys.stdin.read()
    except Exception:
        _passthrough(_NOOP_BAD_STDIN)
    if not raw.strip():
        _passthrough(_NOOP_EMPTY_STDIN)
    try:
        payload = json.loads(raw)
    except Exception:
        _passthrough(_NOOP_BAD_JSON)
    if not isinstance(payload, dict):
        _passthrough(_NOOP_NON_DICT_PAYLOAD)

    # Bash only (the matcher is Bash; double-check so a mis-scoped registration
    # can never rewrite another tool's input).
    if payload.get("tool_name") != "Bash":
        _passthrough(_NOOP_NOT_BASH)

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        _passthrough(_NOOP_BAD_TOOL_INPUT)
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        _passthrough(_NOOP_NO_COMMAND)

    # Loop guard: never double-wrap a command we (any plugin version) rewrote —
    # matches the stable "# furl-pipe" prefix, not one marker spelling.
    if _PIPE_GUARD in command:
        _passthrough(_NOOP_ALREADY_WRAPPED)

    # SECURITY GUARD (provably-safe, total): rewrite ONLY when ZERO readable Bash
    # permission rules exist and settings are fully readable — UNLESS the user set
    # the conscious FURL_PIPE_WITH_RULES override (see the module docstring: no
    # rule-present subset is provably safe). The override short-circuits the scan
    # entirely, so an opted-in user pays no settings I/O either; without it, ANY
    # Bash rule (deny/ask/allow) or unreadable settings (doubt) forces passthrough,
    # so CC's rules apply exactly as native and no command shape can mask a rule. The
    # concrete-rule reason wins the label over doubt when both hold — more actionable.
    if not _pipe_with_rules_override(os.environ.get(_WITH_RULES_ENV)):
        raw_cwd = payload.get("cwd")
        guard_cwd = raw_cwd if isinstance(raw_cwd, str) and raw_cwd.strip() else os.getcwd()
        scan = _scan_bash_rules(guard_cwd)
        if scan.rule_count > 0:
            _passthrough(_NOOP_PERMISSION_RULES)
        if scan.doubt:
            _passthrough(_NOOP_SETTINGS_DOUBT)

    compressor = str(Path(__file__).resolve().parent / "pipe_compress.py")
    rewritten = _rewrite_command(command, _project_dir(payload), compressor)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": {**tool_input, "command": rewritten},
        }
    }
    try:
        sys.stdout.write(json.dumps(output))
    except Exception:
        _passthrough(_NOOP_EMIT_ERROR)
    sys.exit(0)


if __name__ == "__main__":
    # Last-resort guard: no uncaught exception may reach the host — fail open to the
    # original command (emit nothing, exit 0). Not silent: leave one stderr
    # breadcrumb (user-visible, never model-visible) so an unexpected failure is
    # diagnosable, per the repo's no-silent-swallow doctrine.
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:
        try:
            sys.stderr.write(
                f"furl-pipe: unexpected error, command passed through unchanged "
                f"[{exc.__class__.__name__}]\n"
            )
        except Exception:
            pass
        sys.exit(0)
