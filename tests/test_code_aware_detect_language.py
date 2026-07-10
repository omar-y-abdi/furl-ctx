"""Behavioral tests for the public ``detect_language`` heuristic.

``detect_language`` is the public language-detection entry of the code-aware
compressor and had no direct tests. It runs a regex pre-filter (with two
superset disambiguations — TypeScript ⊃ JavaScript, C++ ⊃ C) and, when the
optional tree-sitter grammar pack is installed, a parse-based refinement.

These tests pin the observable contract that holds either way: each supported
language is detected from its own syntax, empty / no-signal input is UNKNOWN
with exactly 0.0 confidence, a match carries positive confidence in (0, 1], and
the superset language wins over its subset when it carries the stronger signal.
The detected language is derived from the input's syntax, not read back from an
internal score.
"""

from __future__ import annotations

import pytest

from furl_ctx.transforms.code_aware_compressor import CodeLanguage, detect_language

_PYTHON = "import os\ndef foo():\n    pass\nclass Bar:\n    pass\n"
_JAVASCRIPT = "const x = 1;\nfunction foo() {}\nlet y = 2;\nvar z = 3;\n"
_TYPESCRIPT = "interface Foo {}\ntype Bar = string;\nenum E { A }\nconst x: number = 1;\n"
_GO = "package main\nfunc main() {}\ntype T struct {}\n"
_RUST = "use std::io;\nfn main() {}\nstruct S;\nimpl S {}\n#[derive(Debug)]\n"
_JAVA = "package com.x;\npublic class Foo {}\n"
_C = "#include <stdio.h>\nint main() {}\ntypedef int myint;\n"
_CPP = "#include <vector>\nnamespace app {}\nstd::vector<int> v;\napp::run();\n"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (_PYTHON, CodeLanguage.PYTHON),
        (_JAVASCRIPT, CodeLanguage.JAVASCRIPT),
        (_TYPESCRIPT, CodeLanguage.TYPESCRIPT),
        (_GO, CodeLanguage.GO),
        (_RUST, CodeLanguage.RUST),
        (_JAVA, CodeLanguage.JAVA),
        (_C, CodeLanguage.C),
        (_CPP, CodeLanguage.CPP),
    ],
)
def test_detects_each_language_from_its_syntax(code: str, expected: CodeLanguage) -> None:
    """Each language sample is detected as itself, with positive confidence."""
    lang, confidence = detect_language(code)
    assert lang is expected
    assert 0.0 < confidence <= 1.0


@pytest.mark.parametrize("blank", ["", "   ", "\n\n  \n", "\t"])
def test_empty_or_whitespace_is_unknown_zero_confidence(blank: str) -> None:
    """Empty / whitespace-only input is UNKNOWN with exactly 0.0 confidence."""
    assert detect_language(blank) == (CodeLanguage.UNKNOWN, 0.0)


def test_no_recognizable_pattern_is_unknown_zero_confidence() -> None:
    """Prose with no language signal scores nothing -> UNKNOWN, 0.0."""
    assert detect_language("the quick brown fox jumps over 12345 things!") == (
        CodeLanguage.UNKNOWN,
        0.0,
    )


def test_typescript_specific_syntax_wins_over_javascript() -> None:
    """TypeScript-only syntax (interface/type/enum/type-annotations) detects as TS, not JS."""
    assert detect_language(_TYPESCRIPT)[0] is CodeLanguage.TYPESCRIPT


def test_cpp_specific_syntax_wins_over_c() -> None:
    """C++-only syntax (namespace, ``::``) detects as C++, while a plain C file stays C."""
    assert detect_language(_CPP)[0] is CodeLanguage.CPP
    assert detect_language(_C)[0] is CodeLanguage.C
