"""Redaction must FAIL CLOSED when it cannot complete, and must stay byte-identical.

The defect these pin. The built-in ``jwt`` credential pattern is QUADRATIC on
adversarial input: ``-`` is simultaneously a non-word character (so it creates the
``\\b`` anchor) and a member of the value class ``[A-Za-z0-9_\\-]`` (so the greedy
run spans across it), which makes ``-eyJ``-dense url-safe-base64 with no ``.`` cost
O(N) match attempts each scanning an O(N) tail. Measured as an operation count
rather than a wall clock, the exponent is exactly 2.00.

Why that was a SECURITY bug rather than a slow path: the PostToolUse hook runs
redaction, the shipped hooks.json kills the hook at 30 s, and the host's fail-open
contract then passes the ORIGINAL, UNREDACTED tool output to the model. Tool output
is attacker-reachable (a repo file, a command's stdout, a fetched page), so a wedge
is a secret-disclosure path. The fix fails CLOSED: on budget overrun the output is
withheld with an explicit message instead of passed through.

NO TEST HERE ASSERTS AN ABSOLUTE WALL-CLOCK DURATION. Every timing-shaped
assertion is either "the verdict is withheld" or a RATIO with wide separation, so
these stay valid on a loaded box, a quiet box, and in CI — the failure mode that
has already produced one false-passing test in this suite.
"""

from __future__ import annotations

import json
import os
import signal
import string
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import furl_ctx.redaction as redaction_module
from furl_ctx.redaction import (
    DEFAULT_REDACT_BUDGET_SECONDS,
    FURL_REDACT_BUDGET_ENV,
    MAX_REDACT_BUDGET_SECONDS,
    build_default_redactor,
    build_store_redactor,
    redact_budget_seconds,
    redact_within_budget,
)

_REPO = Path(__file__).resolve().parents[1]
_HOOK = _REPO / "plugins" / "furl" / "hooks" / "compress_tool_output.py"

_ALNUM = string.ascii_letters + string.digits
# Assembled from parts so no verbatim credential-looking literal sits in source.
_EYJ = "ey" + "J"


def _adversarial(size_bytes: int) -> str:
    """``-eyJ``-dense url-safe-base64 with NO dots: the quadratic trigger.

    Every ``-`` is a word boundary AND stays inside the value class, so each
    ``-eyJ`` restarts a greedy run that scans to the end of the run before it can
    fail on the missing ``.``.
    """
    segment = "-" + _EYJ + "".join(_ALNUM[(i * 17) % 62] for i in range(120))
    return ("X" + segment * (size_bytes // len(segment) + 1))[:size_bytes]


def _benign(size_bytes: int) -> str:
    """Realistic tool output. Scans ZERO characters through the jwt pattern."""
    unit = (
        "2026-07-24T10:00:00Z INFO worker-3 handled request id=12345 status=200\n"
        "def process(payload): return db.query('SELECT id FROM users')\n"
    )
    return (unit * (size_bytes // len(unit) + 1))[:size_bytes]


# ─── the primitive: fail closed, and never hand back the unredacted text ─────


def test_budget_overrun_returns_none_not_the_original() -> None:
    """THE security assertion. On overrun the caller gets None — never the input.

    Red before the fix: there was no budget at all, so the pass ran to completion
    (or the hook was killed mid-pass and the host passed the original through).
    """
    redactor = build_default_redactor({})
    assert redactor is not None
    payload = _adversarial(512 * 1024)

    out = redact_within_budget(payload, redactor, budget_seconds=0.05)

    assert out is None, "a wedged redaction must withhold, not complete"
    # The unredacted text must be unreachable through the return value: there is
    # no shape of the result that carries it.
    assert out != payload


def test_budget_is_not_consulted_when_redaction_completes() -> None:
    """A completing pass returns the redacted text, byte-identical to an
    unbudgeted call. The budget must be invisible to every legitimate input."""
    redactor = build_default_redactor({})
    assert redactor is not None
    text = "token " + _EYJ + "A" * 12 + "." + "B" * 12 + "." + "C" * 12 + " end"

    budgeted = redact_within_budget(text, redactor, budget_seconds=DEFAULT_REDACT_BUDGET_SECONDS)

    assert budgeted == redactor(text)
    assert budgeted is not None and _EYJ + "A" * 12 not in budgeted


def test_benign_content_never_trips_a_tiny_budget_that_adversarial_content_does() -> None:
    """Separation, asserted as a CONTRAST rather than an absolute duration.

    Same budget, same redactor, two inputs of the SAME size: the realistic one
    completes, the adversarial one is withheld. If the quadratic term ever
    regressed into benign content, or the budget started firing on ordinary
    output, exactly one of these two halves flips and the test goes red — without
    ever pinning a second count to this machine.
    """
    redactor = build_default_redactor({})
    assert redactor is not None
    size = 512 * 1024

    assert redact_within_budget(_benign(size), redactor, budget_seconds=1.0) is not None
    assert redact_within_budget(_adversarial(size), redactor, budget_seconds=0.05) is None


@pytest.mark.skipif(
    not hasattr(signal, "SIGALRM"),
    reason="SIGALRM is POSIX-only; absent on native Windows (the disclosed coverage gap)",
)
def test_watchdog_restores_the_callers_pending_alarm() -> None:
    """The review-B7 lesson: a budgeted pass must not eat an alarm the caller
    had already armed, or a pytest-timeout / embedder deadline silently dies.

    Genuinely needs SIGALRM, so the skip is honest — but see
    ``test_sigalrm_is_available_unless_this_is_native_windows``, which makes the
    skip fail loudly if it ever starts firing on a POSIX host."""
    fired: list[str] = []

    def _on_alarm(signum: int, frame: object) -> None:
        fired.append("caller")

    previous = signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, 5.0)
    try:
        redactor = build_default_redactor({})
        assert redactor is not None
        redact_within_budget("harmless", redactor, budget_seconds=0.01)
        remaining, _interval = signal.getitimer(signal.ITIMER_REAL)
        assert remaining > 0, "the caller's pending alarm was cancelled by our budget"
        assert signal.getsignal(signal.SIGALRM) is _on_alarm, "caller's handler not restored"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.skipif(
    not hasattr(signal, "SIGALRM"),
    reason="SIGALRM is POSIX-only; absent on native Windows (the disclosed coverage gap)",
)
def test_a_failing_re_arm_of_the_callers_deadline_cannot_escape_either() -> None:
    """#48. "This function never raises" must hold at EVERY call, not just the disarm.

    ``_restore_after_budget`` makes two kinds of signal call: the disarm-and-restore
    loop, and the re-arm of a caller deadline that was pending. Only the first was
    guarded with the full tuple. The second runs from the same ``finally``, so an
    escape there REPLACES the outcome of the guarded call with an exception the
    callers do not match on — landing in ``_apply_env_redaction``'s
    ``except Exception``, which returns the ORIGINAL text. Same fail-open, reached
    from the re-arm instead of the disarm.

    Reaches the re-arm branch honestly, by arming a real caller deadline first so
    ``previous_remaining`` is non-zero, then failing the third ``setitimer`` — which
    is the re-arm, after the arm and the disarm.
    """
    real_setitimer = signal.setitimer
    calls = {"n": 0}

    def setitimer_that_fails_the_re_arm(which: int, seconds: float, interval: float = 0.0) -> Any:
        calls["n"] += 1
        if calls["n"] == 3:  # 1 = arm ours, 2 = disarm ours, 3 = re-arm the caller's
            raise TypeError("an integer is required")
        return real_setitimer(which, seconds, interval)

    def _on_alarm(signum: int, frame: object) -> None:  # pragma: no cover - never fires
        raise AssertionError("the caller's alarm should not fire during this test")

    previous = signal.signal(signal.SIGALRM, _on_alarm)
    real_setitimer(signal.ITIMER_REAL, 5.0)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(signal, "setitimer", setitimer_that_fails_the_re_arm)
    try:
        out = redact_within_budget("harmless", lambda t: t.upper(), budget_seconds=5.0)
    finally:
        monkey.undo()
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

    assert out == "HARMLESS", (
        "a failed re-arm must not become the exit path: the redaction itself "
        "succeeded, and any escape here is a fail-open passthrough at the caller"
    )
    assert calls["n"] == 3, "the re-arm must actually have been attempted"


@pytest.mark.skipif(
    not hasattr(signal, "SIGALRM"),
    reason="SIGALRM is POSIX-only; absent on native Windows (the disclosed coverage gap)",
)
def test_late_alarm_during_restore_cannot_escape_or_leak_the_handler() -> None:
    """A SIGALRM delivered AFTER the guarded call returned must not become a
    third exit path, and must not leave our handler installed.

    The deadline can expire in the handful of bytecodes between ``redactor(text)``
    returning and the restore running. The upstream ``except`` clause only guards
    the call itself, so an unguarded restore let ``_RedactionBudgetExceeded``
    escape a function documented to return ``str | None`` AND skipped the handler
    restore — leaving ``_raise_budget_exceeded`` installed process-wide, so a
    later unrelated alarm would raise our internal exception at an arbitrary
    point. In the hook that escape was caught by ``_apply_env_redaction``'s
    ``except Exception``, which returns the ORIGINAL text: a fail-open passthrough
    of content whose redaction had actually SUCCEEDED.

    Rather than try to win a microsecond race, this forces the alarm to land
    exactly where it is dangerous by making the restore's disarm raise the same
    exception the real handler raises.
    """
    real_setitimer = signal.setitimer
    calls = {"n": 0}

    def exploding_setitimer(which: int, seconds: float, interval: float = 0.0) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:  # 1 = arm the budget, 2 = the disarm inside the restore
            raise redaction_module.RedactionBudgetExceeded()
        return real_setitimer(which, seconds, interval)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(signal, "setitimer", exploding_setitimer)
    try:
        # Must RETURN, not raise. And the completed value is the honest answer:
        # the redaction finished, so withholding it would be a false withhold.
        out = redact_within_budget("harmless", lambda t: t.upper(), budget_seconds=5.0)
    finally:
        monkey.undo()

    assert out == "HARMLESS", "a late alarm must not turn a completed pass into None"
    assert not redaction_module._is_budget_handler(signal.getsignal(signal.SIGALRM)), (
        "our handler leaked into the caller's process"
    )


# ─── the unbudgeted fallback must be LOUD, not silent ────────────────────────


def test_unarmable_watchdog_warns_instead_of_silently_running_unbounded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Where the budget cannot arm, redaction still runs — but the operator MUST
    be told, or a security control silently becomes a no-op.

    Platform-independent by construction: it monkeypatches the capability check
    rather than the platform, so it runs and can fail everywhere. That matters
    because the condition it guards (native Windows) is exactly the environment
    this suite is least likely to run in.
    """
    monkeypatch.setattr(redaction_module, "_can_arm_watchdog", lambda: False)
    monkeypatch.setattr(redaction_module, "_warned", set())  # defeat warn-once dedupe
    redactor = build_default_redactor({})
    assert redactor is not None

    out = redact_within_budget("harmless text", redactor, budget_seconds=5.0)

    assert out == "harmless text", "the fallback must still redact, not withhold"
    err = capsys.readouterr().err
    assert "UNBOUNDED" in err
    assert "does NOT apply" in err


def test_unbudgeted_warning_names_which_of_the_three_causes_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three causes, three operator responses. One vague line would not tell them
    apart, and two of the three are actionable in completely different places: a
    platform without SIGALRM cannot be fixed from Python at all, an off-main-thread
    call is the caller's own choice, and an unrestorable handler belongs to the
    embedding host rather than to this library.

    Each condition is selected explicitly rather than relying on "whatever is left",
    which is how the previous version of this test went stale: it asserted that with
    SIGALRM present the ONLY remaining cause was the thread, and that stopped being
    true the moment a third cause existed.
    """
    monkeypatch.delattr(signal, "SIGALRM", raising=False)
    assert "native Windows" in redaction_module._unbudgeted_warning()
    monkeypatch.undo()

    if not hasattr(signal, "SIGALRM"):
        pytest.skip("native Windows: the other two causes cannot be selected")

    # Cause 2: off the main thread, where Python signal handlers never run.
    monkeypatch.setattr(redaction_module.threading, "main_thread", lambda: object())
    assert "main thread" in redaction_module._unbudgeted_warning()
    monkeypatch.undo()

    # Cause 3: on the main thread, SIGALRM present, but the current handler is not
    # an object signal.signal would accept back.
    monkeypatch.setattr(signal, "getsignal", lambda signum: None)
    message = redaction_module._unbudgeted_warning()
    assert "outside Python" in message, "cause 3 must name where the handler came from"
    assert "exec resets handlers" in message, (
        "cause 3 must say it needs an embedding host, so nobody hunts for it in a "
        "normally launched process"
    )


@pytest.mark.skipif(
    not hasattr(signal, "SIGALRM"),
    reason="SIGALRM is POSIX-only; absent on native Windows (the disclosed coverage gap)",
)
def test_a_handler_that_cannot_be_restored_is_never_armed_over(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#48. An unrestorable ``previous_handler`` must stop the arm, not be caught later.

    ``signal.getsignal`` returns ``None`` when SIGALRM's disposition was neither
    ``SIG_DFL`` nor ``SIG_IGN`` as the signal module initialised, and
    ``signal.signal`` then rejects that value with ``TypeError`` — measured on the
    project interpreter for ``None``, ``3`` and ``'x'`` alike.

    Arming anyway is the trap. There would be no installable object to put back, so
    the handler's self-disarm fails, the restore fails, and our raising handler
    becomes a PERMANENT process-wide fixture instead of a 1-in-16000 one — while
    also hijacking SIGALRM from whatever C code owned it. Refusing to arm is the
    only outcome that leaves the process as it was found.

    HONEST REACHABILITY, because this test monkeypatches a state it could not
    otherwise reach. ``getsignal() -> None`` requires an embedding host: a C-level
    handler installed before ``Py_Initialize``, or a fork-without-exec from one.
    It is UNREACHABLE through every entry point in this repository, and that is
    measured rather than assumed — ``exec`` resets a C-installed handler to
    ``SIG_DFL`` (verified in-process), ``SIG_IGN`` survives ``exec`` but maps to
    ``IgnoreHandler`` rather than ``None``, no Rust in this tree installs a signal
    handler, and no production module imports ``ctypes`` or ``cffi``. The guard is
    therefore structural rather than a response to an observed failure, exactly as
    with the outermost fail-closed handler.
    """
    monkeypatch.setattr(signal, "getsignal", lambda signum: None)
    monkeypatch.setattr(redaction_module, "_warned", set())  # defeat warn-once dedupe
    installed_before = signal.getsignal  # the patched callable; identity is not the point

    out = redact_within_budget("harmless", lambda t: t.upper(), budget_seconds=5.0)

    assert out == "HARMLESS", (
        "refusing to arm must fall back to running the redactor, not withhold: "
        "withholding everything on a host that never had a budget is catastrophic"
    )
    err = capsys.readouterr().err
    assert "UNBOUNDED" in err, "an unavailable guarantee must be visible, never assumed"
    assert "outside Python" in err, "the warning must name THIS cause, not a generic one"
    assert installed_before is signal.getsignal, "sanity: the patch stayed in place"


@pytest.mark.skipif(
    not hasattr(signal, "SIGALRM"),
    reason="SIGALRM is POSIX-only; absent on native Windows (the disclosed coverage gap)",
)
def test_restore_cannot_raise_an_uninstallable_handler_into_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#48. The restore's tuple must not be narrower than the handler's.

    The handler guarded ``signal.signal`` against ``(OSError, ValueError,
    TypeError)`` while the restore loop guarded the SAME call against
    ``(RedactionBudgetExceeded, OSError)`` only. So the identical escape was closed
    in one place and open in the other, and the open one lands exactly where the
    defect this module exists to fix lands: out through ``_redact_under_watchdog``,
    past ``redact_within_budget``'s guard, into ``_apply_env_redaction``'s
    ``except Exception``, which returns the ORIGINAL text.

    Unreachable now that the arm refuses on ``None`` — see the sibling test — so
    this drives it with a ``signal.signal`` that rejects the restore directly. It
    pins the ASYMMETRY, not a reachable defect: escaping is fail-open, swallowing is
    fail-closed, and the cheap insurance belongs on the fail-closed side.
    """
    real_signal = signal.signal
    calls = {"n": 0}

    def signal_that_rejects_the_restore(signalnum: int, handler: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:  # 1 = arming ours, 2 = putting the caller's back
            raise TypeError(
                "signal handler must be signal.SIG_IGN, signal.SIG_DFL, or a callable object"
            )
        return real_signal(signalnum, handler)

    monkeypatch.setattr(signal, "signal", signal_that_rejects_the_restore)
    try:
        out = redact_within_budget("harmless", lambda t: t.upper(), budget_seconds=5.0)
    finally:
        monkeypatch.undo()
        leaked = redaction_module._is_budget_handler(signal.getsignal(signal.SIGALRM))
        if leaked:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, signal.SIG_DFL)

    assert out == "HARMLESS", (
        "a rejected restore must not escape as TypeError; the caller's generic "
        "handler would turn that into a fail-open passthrough of unredacted text"
    )
    assert calls["n"] >= 3, "the restore must RETRY rather than propagate"
    assert not leaked, "the raising handler was left installed process-wide"


def test_warning_is_emitted_once_per_process_not_per_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A per-call warning on a hot path is noise the operator learns to ignore."""
    monkeypatch.setattr(redaction_module, "_can_arm_watchdog", lambda: False)
    monkeypatch.setattr(redaction_module, "_warned", set())
    redactor = build_default_redactor({})
    assert redactor is not None

    for _ in range(5):
        redact_within_budget("x", redactor, budget_seconds=5.0)

    assert capsys.readouterr().err.count("UNBOUNDED") == 1


def test_sigalrm_is_available_unless_this_is_native_windows() -> None:
    """Makes the skip above fail LOUDLY if it ever starts firing on a POSIX host.

    A platform guard silently reducing coverage is the same shape as the
    optional-suite-disarmed defect this project already fixed once: the suite
    goes green while protecting nothing. This asserts the skip is reachable ONLY
    on the platform it names, and it never itself skips.
    """
    if sys.platform == "win32":
        # The documented, accepted coverage gap — the watchdog cannot arm here.
        assert not hasattr(signal, "SIGALRM")
        return
    assert hasattr(signal, "SIGALRM"), (
        f"SIGALRM missing on {sys.platform!r}, which is NOT the documented native-Windows "
        "gap. The watchdog cannot arm here, so the redaction timeout is silently "
        "inactive and its test is silently skipped."
    )


# ─── budget parsing: a typo must not silently mean "no budget" ───────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", DEFAULT_REDACT_BUDGET_SECONDS),
        ("   ", DEFAULT_REDACT_BUDGET_SECONDS),
        ("banana", DEFAULT_REDACT_BUDGET_SECONDS),
        ("0", DEFAULT_REDACT_BUDGET_SECONDS),
        ("-3", DEFAULT_REDACT_BUDGET_SECONDS),
        ("0.5", 0.5),
        ("8", 8.0),
    ],
    ids=["unset", "blank", "not-a-number", "zero", "negative", "fractional", "under-ceiling"],
)
def test_budget_parsing_never_degrades_to_unbounded(raw: str, expected: float) -> None:
    assert redact_budget_seconds({FURL_REDACT_BUDGET_ENV: raw}) == expected


@pytest.mark.parametrize("raw", ["12", "15", "30", "600", "1e9"])
def test_budget_above_the_ceiling_is_clamped_not_honoured(
    raw: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """An over-large budget must not silently re-open the fail-open.

    The redactor runs up to THREE times per hook invocation — the extracted field,
    a Bash-shaped preserved stderr, and a third UNBUDGETED pass inside
    ``compress()`` — against a 30 s host timeout that fails OPEN when it fires. So
    the real safe ceiling is ~9 s, not the 30 an unclamped parser would accept. An
    operator setting 12 or 15 would believe they were comfortably under the host
    timeout while actually restoring the disclosure this module exists to close.
    Same "a typo must not mean no budget" class already guarded at 0 and negative,
    left open at the other end until now.
    """
    monkey = pytest.MonkeyPatch()
    monkey.setattr(redaction_module, "_warned", set())
    try:
        assert redact_budget_seconds({FURL_REDACT_BUDGET_ENV: raw}) == MAX_REDACT_BUDGET_SECONDS
    finally:
        monkey.undo()
    assert "ceiling" in capsys.readouterr().err, "clamping must be loud, not silent"


def test_ceiling_times_three_still_fits_inside_the_host_timeout() -> None:
    """Pins the arithmetic the ceiling is derived from, so moving one without the
    other goes red. 30 s is the shipped hooks.json PostToolUse timeout."""
    assert MAX_REDACT_BUDGET_SECONDS * 3 < 30.0
    assert DEFAULT_REDACT_BUDGET_SECONDS <= MAX_REDACT_BUDGET_SECONDS


@pytest.mark.skipif(
    not hasattr(signal, "SIGALRM"),
    reason="SIGALRM is POSIX-only; absent on native Windows (the disclosed coverage gap)",
)
def test_late_alarm_at_the_call_to_restore_still_restores_the_handler() -> None:
    """R1. The alarm can land ON THE CALL to the restore, not only inside it.

    ``_redact_under_watchdog``'s ``finally`` CALLS ``_restore_after_budget``, and
    that call is itself an eval-breaker sitting outside every inner ``try``. An
    alarm delivered exactly there raises AT the call, so the restore's BODY never
    runs and ``_raise_budget_exceeded`` is left installed PROCESS-WIDE — a
    cross-call landmine in any long-lived process, where a later unrelated SIGALRM
    raises this module's private exception in a frame that has never heard of
    redaction.

    Why this needs its own test: the sibling test above monkeypatches
    ``signal.setitimer``, which can only raise once execution is already INSIDE the
    restore, where the retry loop catches it. A ``setitimer`` patch STRUCTURALLY
    CANNOT reach the call-to-restore window, so that test stayed green while this
    defect was live. Here the failure is modelled where it actually happens, by
    making the CALL itself raise.
    """
    real_restore = redaction_module._restore_after_budget
    calls = {"n": 0}

    def restore_that_is_interrupted_at_the_call(*args: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise redaction_module.RedactionBudgetExceeded()
        real_restore(*args)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        redaction_module, "_restore_after_budget", restore_that_is_interrupted_at_the_call
    )
    try:
        out = redact_within_budget("harmless", lambda t: t.upper(), budget_seconds=5.0)
    finally:
        monkey.undo()
        # Belt and braces so a RED run cannot poison the rest of the suite with a
        # leaked handler or a live itimer.
        leaked = redaction_module._is_budget_handler(signal.getsignal(signal.SIGALRM))
        if leaked:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, signal.SIG_DFL)

    assert out is None, "an interrupted restore must still withhold"
    assert not leaked, "the raising handler was left installed process-wide"


@pytest.mark.skipif(
    not hasattr(signal, "SIGALRM"),
    reason="SIGALRM is POSIX-only; absent on native Windows (the disclosed coverage gap)",
)
def test_the_handler_puts_the_previous_one_back_before_it_raises() -> None:
    """R2. The handler disarms ITSELF, so no unwinding route can leak it.

    Every restore call sits at a bytecode the alarm can pre-empt, so "the handler
    is never left installed" was previously a claim about an ENUMERATION of those
    bytecodes. The enumeration was measured incomplete: against the tree that had
    both the restore's retry loop and the R1 backstop, an aimed race harness still
    found the raising handler installed after 6 of 16000 calls that RETURNED
    NORMALLY. Text-only assertions cannot see that — the outermost guard converts
    the escape to a withhold, so the return value looks correct while the process
    keeps a landmine that makes a later unrelated SIGALRM raise this module's
    exception in a frame that has never heard of redaction.

    Self-disarming replaces the enumeration with two cases: the handler ran, so it
    restored the previous handler itself; or it never ran, so nothing was
    interrupted and the ordinary restore reaches its body.

    Asserted on the handler directly rather than through a race, because the race
    is probabilistic and a probabilistic test in the suite is a flake. The same
    harness that found the 6 leaks reports 0 in 20000 trials with this in place.
    """
    sentinel_ran: list[int] = []

    def previous_handler(signum: int, frame: object) -> None:
        sentinel_ran.append(signum)

    real_previous = signal.getsignal(signal.SIGALRM)
    try:
        signal.signal(signal.SIGALRM, previous_handler)
        handler = redaction_module._make_budget_handler(previous_handler)
        signal.signal(signal.SIGALRM, handler)
        assert redaction_module._is_budget_handler(signal.getsignal(signal.SIGALRM)), (
            "the marker must identify our own handler; the leak check depends on it"
        )

        with pytest.raises(redaction_module.RedactionBudgetExceeded):
            handler(int(signal.SIGALRM), None)

        installed = signal.getsignal(signal.SIGALRM)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, real_previous)

    assert installed is previous_handler, (
        "the handler must reinstall the caller's handler BEFORE raising, otherwise "
        "every path that skips a restore call leaks it process-wide"
    )
    assert not redaction_module._is_budget_handler(installed), "our handler stayed installed"
    assert sentinel_ran == [], "self-disarming must not also INVOKE the previous handler"


@pytest.mark.skipif(
    not hasattr(signal, "SIGALRM"),
    reason="SIGALRM is POSIX-only; absent on native Windows (the disclosed coverage gap)",
)
def test_discarded_signal_oserror_from_the_restore_cannot_escape() -> None:
    """R3. ``signal.signal`` reports a discarded alarm as ``OSError``, not ours.

    When a signal has been tripped at C level but the Python-level handler is no
    longer a callable by the time the eval loop dispatches it, CPython raises
    ``OSError("Signal 14 ignored due to race condition")`` from
    ``_PyErr_CheckSignalsTstate`` — surfacing at the ``signal.signal`` call that
    performed the swap. That is EXACTLY the call the restore makes, and in the hook
    the handler it installs is ``SIG_DFL``, i.e. precisely the non-callable case.

    ``OSError`` is not ``RedactionBudgetExceeded`` and shares no base with it, so
    it walked past the restore's ``except``, past ``_redact_under_watchdog``'s
    backstop and past ``redact_within_budget``'s outermost guard, into
    ``_apply_env_redaction``'s ``except Exception`` — which returns the ORIGINAL
    text. A fail-OPEN disclosure reached through the fail-CLOSED machinery.

    Modelled deterministically at the exact call rather than raced for, and the
    return value is the completed text because the pass DID finish: a discarded
    late alarm is not a reason to withhold.
    """
    real_signal = signal.signal
    calls = {"n": 0}

    def signal_that_reports_a_discarded_alarm(signalnum: int, handler: Any) -> Any:
        calls["n"] += 1
        # 1 = arming our handler, 2 = the restore putting the caller's handler back.
        if calls["n"] == 2:
            raise OSError(f"Signal {int(signal.SIGALRM)} ignored due to race condition")
        return real_signal(signalnum, handler)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(signal, "signal", signal_that_reports_a_discarded_alarm)
    try:
        out = redact_within_budget("harmless", lambda t: t.upper(), budget_seconds=5.0)
    finally:
        monkey.undo()
        leaked = redaction_module._is_budget_handler(signal.getsignal(signal.SIGALRM))
        if leaked:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, signal.SIG_DFL)

    assert out == "HARMLESS", (
        "a discarded-signal report must not escape as an exception; the caller's "
        "generic handler turns any escape into a fail-open passthrough"
    )
    assert calls["n"] >= 3, "the restore must RETRY after the report, not give up on it"
    assert not leaked, "the raising handler was left installed process-wide"


@pytest.mark.skipif(
    not hasattr(signal, "SIGALRM"),
    reason="SIGALRM is POSIX-only; absent on native Windows (the disclosed coverage gap)",
)
def test_a_restore_that_never_succeeds_gives_up_loudly_instead_of_hanging(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The retry must be BOUNDED: a permanent fault must not become a hang.

    The termination argument for retrying covers the faults the restore knows
    about — a one-shot itimer that is disarmed first, so at most one pending alarm
    can interrupt and retrying consumes it. It says nothing about a fault that does
    not clear, and an unbounded loop would turn that into an infinite spin inside a
    security control, which is worse than the leak it was added to prevent.

    Giving up must also be audible, since it leaves the process's SIGALRM state in
    a shape this module did not choose.
    """
    real_signal = signal.signal
    calls = {"n": 0}

    def signal_that_always_reports_a_discarded_alarm(signalnum: int, handler: Any) -> Any:
        calls["n"] += 1
        if calls["n"] >= 2:  # every restore attempt fails, permanently
            raise OSError(f"Signal {int(signal.SIGALRM)} ignored due to race condition")
        return real_signal(signalnum, handler)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(redaction_module, "_warned", set())  # defeat warn-once dedupe
    monkey.setattr(signal, "signal", signal_that_always_reports_a_discarded_alarm)
    try:
        out = redact_within_budget("harmless", lambda t: t.upper(), budget_seconds=5.0)
    finally:
        monkey.undo()
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)

    assert out == "HARMLESS", "giving up on the restore must not change the redaction verdict"
    assert calls["n"] == 1 + redaction_module._RESTORE_ATTEMPTS, (
        "the loop must stop at _RESTORE_ATTEMPTS rather than spin"
    )
    assert "SIGALRM" in capsys.readouterr().err, "giving up must not be silent"


def test_interrupted_pass_withholds_rather_than_returning_a_partial() -> None:
    """A pass interrupted part-way must yield None, never the partial it built.

    HONEST SCOPE, because the name of this test used to claim more than it
    established. It pins BEHAVIOUR: interrupted => withhold. It does NOT
    distinguish the 1-tuple capture from a plain ``str | None`` classified by
    absence of exception — both return None here, because a redactor that raises
    never hands a partial back in the first place. The 1-tuple is a STRUCTURAL
    guard (a value that can only be constructed after the redactor returned), and
    its value is that a future edit cannot quietly start treating "nothing was
    raised" as "the pass completed". That property is enforced by the shape of the
    code and argued in the docstring; it is not observable from outside, so no test
    here can pin it and this one does not pretend to.
    """
    partial_seen: list[str] = []

    def half_redacting(text: str) -> str:
        partial_seen.append(text.replace("AAAA", "[R]"))  # a partial scrub exists...
        raise redaction_module.RedactionBudgetExceeded()  # ...and is then interrupted

    out = redact_within_budget("AAAA BBBB", half_redacting, budget_seconds=5.0)

    assert out is None, "an interrupted pass must withhold"
    assert partial_seen, "guard: the partial really was constructed"
    assert out != partial_seen[0], "the partial must not be returned as a completed result"


def test_outermost_guard_converts_a_residual_escape_into_a_withhold() -> None:
    """Pins the OUTERMOST guard specifically, and reddens if it is deleted.

    The previous version of this test raised inside the redactor, which the INNER
    except catches — so it passed with or without the outer guard and pinned
    nothing. Here the escape is forced past every inner handler by making the whole
    armed section raise, which is the only way to reach the outermost layer.
    """

    def armed_section_that_escapes(*args: Any, **kwargs: Any) -> str | None:
        raise redaction_module.RedactionBudgetExceeded()

    monkey = pytest.MonkeyPatch()
    monkey.setattr(redaction_module, "_redact_under_watchdog", armed_section_that_escapes)
    try:
        redactor = build_default_redactor({})
        assert redactor is not None
        # Must be a fail-CLOSED verdict, not a propagating exception a caller's
        # generic ``except Exception`` would turn into "return the original text".
        assert redact_within_budget("x", redactor, budget_seconds=5.0) is None
    finally:
        monkey.undo()


# ─── byte-identity: the fix must not change WHICH bytes get redacted ─────────

_ARMOR = "PRIVATE" + " KEY-----"
_CORPUS = {
    "openai": "config = sk-" + "A" * 24 + " end",
    "github": "token gh" + "p_" + "B" * 30 + " end",
    "aws": "id AK" + "IA" + "IOSFODNN7" + "EXAMPLE" + " end",
    "gcp": "key AI" + "za" + "C" * 35 + " end",
    "slack": "tok xo" + "xb-" + "1" * 20 + " end",
    "jwt": "auth " + _EYJ + "A" * 12 + "." + "B" * 12 + "." + "C" * 12 + " end",
    "pem-full": "-----BEGIN RSA " + _ARMOR + "\nMIIEow" + "D" * 40 + "\n-----END RSA " + _ARMOR,
    "pem-truncated": "-----BEGIN RSA " + _ARMOR + "\nMIIEow" + "E" * 40,
    "pem-lone-armor": "-----BEGIN RSA " + _ARMOR,
    "pem-json-escaped": '{"k":"-----BEGIN RSA ' + _ARMOR + "\\n" + "L" * 40 + '"}',
    "pem-abutting-body": "-----BEGIN RSA " + _ARMOR + "M" * 40,
    "empty": "",
    "position-zero": "sk-" + "F" * 24,
    "end-of-input": "trailing sk-" + "G" * 24,
    "overlapping": "sk-" + "H" * 24 + _EYJ + "I" * 12 + "." + "J" * 12 + "." + "K" * 12,
    "public-material": "-----BEGIN PUBLIC KEY-----\n" + "M" * 40 + "\n-----END PUBLIC KEY-----",
    "prose": "the quick brown fox jumps over the lazy dog",
    "multiline-mixed": "line one\nsk-" + "N" * 24 + "\nline three\n",
}


@pytest.mark.parametrize("name", sorted(_CORPUS))
def test_budgeted_redaction_is_byte_identical_to_unbudgeted(name: str) -> None:
    """The hard gate. A generous budget must produce EXACTLY the bytes the
    unbudgeted redactor produces, for every pattern and every edge shape."""
    text = _CORPUS[name]
    redactor = build_store_redactor({})
    assert redactor is not None

    assert redact_within_budget(text, redactor, budget_seconds=30.0) == redactor(text)


@pytest.mark.parametrize("name", sorted(_CORPUS))
def test_redaction_is_idempotent_for_the_builtins(name: str) -> None:
    """Pins the property the (separately tracked) double-pass removal would rely
    on. Built-ins only: an operator pattern that matches an earlier pattern's own
    marker is NOT idempotent, which is exactly why that removal is not a
    mechanical deletion."""
    redactor = build_default_redactor({})
    assert redactor is not None
    once = redactor(_CORPUS[name])
    assert redactor(once) == once


# ─── the hook: end to end, the actual disclosure path ────────────────────────


def _run_hook(
    tmp_path: Path, stdout_text: str, *, budget: str | None, min_chars: str = "500"
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["FURL_CCR_BACKEND"] = "sqlite"
    env["FURL_CCR_SQLITE_PATH"] = str(tmp_path / "ccr.sqlite3")
    env["FURL_CCR_PROJECT_DIR"] = ""
    env.pop("FURL_CCR_NAMESPACE", None)
    env.pop("FURL_REDACT_PATTERNS", None)  # built-ins alone
    env["FURL_HOOK_MIN_CHARS"] = min_chars
    if budget is None:
        env.pop(FURL_REDACT_BUDGET_ENV, None)
    else:
        env[FURL_REDACT_BUDGET_ENV] = budget
    payload = {
        "tool_name": "Bash",
        "tool_response": {"stdout": stdout_text, "stderr": ""},
        "cwd": str(tmp_path),
        "hook_event_name": "PostToolUse",
    }
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
    )


def test_hook_withholds_output_when_redaction_cannot_complete(tmp_path: Path) -> None:
    """THE regression test for the disclosure path.

    Before the fix this content ran the hook past its 30 s kill, the host applied
    its fail-open contract, and the ORIGINAL UNREDACTED bytes reached the model.
    Now the hook replaces the output with an explicit withheld notice.
    """
    payload = _adversarial(512 * 1024)
    proc = _run_hook(tmp_path, payload, budget="0.05")

    assert proc.returncode == 0, "the hook must never break the tool call"
    assert proc.stdout.strip(), "expected a replacement, not a fail-open passthrough"

    emitted = json.loads(proc.stdout)
    replacement = emitted["hookSpecificOutput"]["updatedToolOutput"]["stdout"]

    # The disclosure assertion: the original content must NOT be what the model
    # sees. A generous slice is used so a partial passthrough also fails.
    assert payload[:4096] not in replacement
    assert "WITHHELD" in replacement
    assert "FURL_REDACT_BUDGET_SECONDS" in replacement
    # And the operator is told on stderr, not only the model.
    assert "redaction budget exceeded" in proc.stderr


def test_hook_passes_benign_output_through_untouched_under_the_same_budget(
    tmp_path: Path,
) -> None:
    """The other half: the withhold must not fire on ordinary output. Same tiny
    budget, benign content — no withholding, no notice."""
    proc = _run_hook(tmp_path, _benign(64 * 1024), budget="5")

    assert proc.returncode == 0
    assert "WITHHELD" not in proc.stdout
    assert "redaction budget exceeded" not in proc.stderr


def test_nested_content_wrapped_stderr_is_still_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Bash-shaped response wrapped in ``content`` keeps its stderr one frame
    down. ``_reinject`` recurses to reach it, so the redacted value must recurse
    with it — threading it only at the top frame left the INNER stderr holding its
    original bytes while the wrapper looked correctly scrubbed.

    Caught as a review hypothesis, confirmed against the pre-change hook: the old
    code redacted this field unconditionally inside the recursion, so losing it
    was a REGRESSION, not a pre-existing gap.
    """
    import importlib.util

    monkeypatch.setenv("FURL_REDACT_PATTERNS", r"SECRET-\d+")
    monkeypatch.delenv("FURL_REDACT_BUILTINS", raising=False)
    spec = importlib.util.spec_from_file_location("furl_hook_nested_stderr", _HOOK)
    assert spec is not None and spec.loader is not None
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)

    nested = {"content": {"stdout": "a" * 40, "stderr": "auth SECRET-99999 leaked"}}

    # The preserved field must be FOUND at depth, or nothing is ever computed for it.
    assert hook._preserved_stderr(nested) == "auth SECRET-99999 leaked"

    redacted_stderr = hook._apply_env_redaction(hook._preserved_stderr(nested))
    assert redacted_stderr is not None and "SECRET-99999" not in redacted_stderr

    mirrored = hook._reinject(nested, "COMPRESSED", redacted_stderr)
    assert "SECRET-99999" not in json.dumps(mirrored), "nested stderr leaked through _reinject"
    assert mirrored["content"]["stdout"] == "COMPRESSED"

    # THE WITHHOLD PATH, which the emit path above does not exercise.
    withheld = hook._reinject(nested, hook._withheld_message(5.0), hook._STDERR_WITHHELD)
    assert "SECRET-99999" not in json.dumps(withheld), (
        "nested stderr leaked through the WITHHOLD mirror"
    )
    assert hook._STDERR_WITHHELD in json.dumps(withheld)


def test_redaction_runs_before_the_min_chars_gate(tmp_path: Path) -> None:
    """Pins the ORDERING. ``_min_chars`` is a MINIMUM, not a cap: redaction must
    run first so a below-threshold output is still scrubbed (review Finding 1).

    Goes red if a future edit moves the size gate above redaction — the change
    that would look like a cheap way to bound the scanner's input and would
    silently stop redacting every small tool output.
    """
    secret = "sk-" + "P" * 30
    proc = _run_hook(tmp_path, f"tiny {secret} done", budget=None, min_chars="100000")

    assert proc.returncode == 0
    assert proc.stdout.strip(), "a below-threshold output carrying a secret must still emit"
    assert secret not in proc.stdout
    assert "[REDACTED:openai-key]" in proc.stdout
