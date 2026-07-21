"""AST-verified, CCR-backed code compression (Engine P2-12) — OPT-IN.

Restored from the archived pre-excision module (``archive/headroom/
transforms/code_compressor.py``) as a gated capability: tree-sitter parses
source code into an AST, function bodies are truncated statement-by-
statement under a relevance-weighted line budget, and the render ships
ONLY if it still parses and the FULL ORIGINAL is retrievable behind the
emitted marker. This is distinct from the retired ast-grep code path
(owner decision Q3) — different engine, different guarantees.

Gating (default behavior unchanged):

* ``ContentRouterConfig.enable_code_aware`` is ``False`` by default —
  SOURCE_CODE keeps routing to PASSTHROUGH exactly as before.
* The router's protections still run FIRST: analysis-intent and
  ``protect_recent_code`` keep code verbatim before routing ever
  reaches this compressor; ``lossless_only`` gates the dispatch arm off.

Optional dependency (lean-core mandate — NOT a default dep)::

    pip install furl-ctx[code]   # tree-sitter + tree-sitter-language-pack

The import is lazy and FAIL-OPEN: when the grammars are missing the
compressor passes content through untouched and logs ONE warning for the
process lifetime.

# Reversibility contract (mirrors text_crusher.py)

Body truncation is lossy-structural — the render alone cannot reproduce
the original bytes. A truncating render therefore ships **if and only
if** the full original is persisted in the production
``CompressionStore`` under the exact hash embedded in the emitted
``{comment} [N lines compressed to M. Retrieve more: hash=…]`` marker
(grammar Shape H). On ANY store failure the compressor serves the
ORIGINAL uncompressed code — the marker never ships dangling. With
``enable_ccr=False`` there is no recovery plane, so nothing lossy ever
ships (passthrough).

# Syntax contract

The shipped render (marker line included — it is a line comment in the
target language) must parse with zero ERROR/MISSING nodes; otherwise the
original is served. Broken code is never worth the savings.

Thread-safety: tree-sitter parsers are ``#[pyclass(unsendable)]`` — they
hard-panic when touched from a foreign thread. The router compresses in a
``ThreadPoolExecutor``, so parsers are held in thread-local storage, one
per (thread, language).

Reference: LongCodeZip — https://arxiv.org/abs/2510.00446
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ._ccr_persist import persist_to_python_ccr

logger = logging.getLogger(__name__)

# ─── Optional-dependency management (lazy, fail-open, warn-once) ────────────

_tree_sitter_available: bool | None = None
_tree_sitter_local = threading.local()

# Process-lifetime latch for the missing-dep warning: the router calls
# compress() per message, and a per-call warning would flood agent logs.
_MISSING_DEP_WARNED = False


def _check_tree_sitter_available() -> bool:
    """Check (and cache) whether the optional tree-sitter packages exist."""
    global _tree_sitter_available
    if _tree_sitter_available is None:
        try:
            import tree_sitter_language_pack  # noqa: F401

            _tree_sitter_available = True
        except ImportError:
            _tree_sitter_available = False
    return _tree_sitter_available


def _warn_missing_dep_once() -> None:
    """Log the fail-open passthrough exactly once per process."""
    global _MISSING_DEP_WARNED
    if not _MISSING_DEP_WARNED:
        _MISSING_DEP_WARNED = True
        logger.warning(
            "code_aware: enable_code_aware is on but tree-sitter is not "
            "installed — passing source code through untouched. Install the "
            "optional extra: pip install furl-ctx[code]"
        )


def _get_parser(language: str) -> Any:
    """Return a **thread-local** ``tree_sitter.Parser`` for *language*.

    tree-sitter ≥ 0.23 wraps the C ``TSParser`` in a PyO3
    ``#[pyclass(unsendable)]`` which hard-panics if the object is accessed
    from any thread other than its creator. The router runs compression
    inside a ``ThreadPoolExecutor``, so a shared parser would be touched
    from arbitrary pool threads → instant crash. One parser per
    (thread, language) satisfies the ``unsendable`` contract with
    negligible extra memory.

    Raises:
        ImportError: If tree-sitter is not installed.
        ValueError: If *language* is not supported.
    """
    if not _check_tree_sitter_available():
        raise ImportError("tree-sitter is not installed. Install with: pip install furl-ctx[code]")

    parsers: dict[str, Any] | None = getattr(_tree_sitter_local, "parsers", None)
    if parsers is None:
        parsers = {}
        _tree_sitter_local.parsers = parsers

    if language not in parsers:
        try:
            from tree_sitter import Parser
            from tree_sitter_language_pack import get_language

            parser = Parser()
            # `language` is a validated runtime str; get_language types its arg
            # as a Literal of supported names, which a dynamic str can't satisfy.
            parser.language = get_language(language)  # type: ignore[arg-type]
            parsers[language] = parser
            logger.debug(
                "Loaded tree-sitter parser for %s (thread %s)",
                language,
                threading.current_thread().name,
            )
        except Exception as e:
            raise ValueError(
                f"Language '{language}' is not supported by tree-sitter. "
                f"Supported: python, javascript, typescript, go, rust, java, c, cpp. "
                f"Error: {e}"
            ) from e

    return parsers[language]


# ─── Language model ──────────────────────────────────────────────────────────


class CodeLanguage(Enum):
    """Supported programming languages."""

    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    GO = "go"
    RUST = "rust"
    JAVA = "java"
    C = "c"
    CPP = "cpp"
    UNKNOWN = "unknown"


class DocstringMode(Enum):
    """How to handle docstrings in compressed bodies."""

    FULL = "full"  # Keep entire docstring
    FIRST_LINE = "first_line"  # Keep only first line
    REMOVE = "remove"  # Remove docstrings completely


@dataclass(frozen=True)
class LangConfig:
    """Data-driven configuration for a programming language.

    Instead of per-language methods, each language declares its AST node
    types and syntactic conventions. The compressor uses these tables to
    drive extraction and compression generically.
    """

    # AST node types for structural extraction
    import_nodes: frozenset[str]
    function_nodes: frozenset[str]
    class_nodes: frozenset[str]
    type_nodes: frozenset[str]
    body_node_types: frozenset[str]  # Node types that represent function/method bodies
    decorator_node: str | None  # e.g. "decorated_definition" for Python

    # Syntax conventions
    comment_prefix: str  # "#" for Python, "//" for C-family
    uses_colon_after_signature: bool  # Python: True, C-family: False
    package_node: str | None = None  # e.g. "package_clause" for Go


_LANG_CONFIGS: dict[CodeLanguage, LangConfig] = {
    CodeLanguage.PYTHON: LangConfig(
        import_nodes=frozenset({"import_statement", "import_from_statement"}),
        function_nodes=frozenset({"function_definition"}),
        class_nodes=frozenset({"class_definition"}),
        type_nodes=frozenset({"type_alias_statement"}),
        body_node_types=frozenset({"block"}),
        decorator_node="decorated_definition",
        comment_prefix="#",
        uses_colon_after_signature=True,
    ),
    CodeLanguage.JAVASCRIPT: LangConfig(
        import_nodes=frozenset({"import_statement", "import_declaration"}),
        function_nodes=frozenset({"function_declaration", "method_definition"}),
        class_nodes=frozenset({"class_declaration"}),
        type_nodes=frozenset(),
        body_node_types=frozenset({"statement_block"}),
        decorator_node=None,
        comment_prefix="//",
        uses_colon_after_signature=False,
    ),
    CodeLanguage.TYPESCRIPT: LangConfig(
        import_nodes=frozenset({"import_statement", "import_declaration"}),
        function_nodes=frozenset({"function_declaration", "method_definition"}),
        class_nodes=frozenset({"class_declaration"}),
        type_nodes=frozenset({"interface_declaration", "type_alias_declaration"}),
        body_node_types=frozenset({"statement_block"}),
        decorator_node=None,
        comment_prefix="//",
        uses_colon_after_signature=False,
    ),
    CodeLanguage.GO: LangConfig(
        import_nodes=frozenset({"import_declaration"}),
        function_nodes=frozenset({"function_declaration", "method_declaration"}),
        class_nodes=frozenset(),
        type_nodes=frozenset({"type_declaration"}),
        body_node_types=frozenset({"block"}),
        decorator_node=None,
        comment_prefix="//",
        uses_colon_after_signature=False,
        package_node="package_clause",
    ),
    CodeLanguage.RUST: LangConfig(
        import_nodes=frozenset({"use_declaration"}),
        function_nodes=frozenset({"function_item"}),
        class_nodes=frozenset({"impl_item"}),
        type_nodes=frozenset({"struct_item", "enum_item", "type_item", "trait_item"}),
        body_node_types=frozenset({"block"}),
        decorator_node=None,
        comment_prefix="//",
        uses_colon_after_signature=False,
    ),
    CodeLanguage.JAVA: LangConfig(
        import_nodes=frozenset({"import_declaration"}),
        function_nodes=frozenset({"method_declaration", "constructor_declaration"}),
        class_nodes=frozenset({"class_declaration", "interface_declaration"}),
        type_nodes=frozenset({"enum_declaration"}),
        body_node_types=frozenset({"block"}),
        decorator_node=None,
        comment_prefix="//",
        uses_colon_after_signature=False,
        package_node="package_declaration",
    ),
    CodeLanguage.C: LangConfig(
        import_nodes=frozenset({"preproc_include"}),
        function_nodes=frozenset({"function_definition"}),
        class_nodes=frozenset(),
        type_nodes=frozenset({"struct_specifier", "enum_specifier", "type_definition"}),
        body_node_types=frozenset({"compound_statement"}),
        decorator_node=None,
        comment_prefix="//",
        uses_colon_after_signature=False,
    ),
    CodeLanguage.CPP: LangConfig(
        import_nodes=frozenset({"preproc_include"}),
        function_nodes=frozenset({"function_definition"}),
        class_nodes=frozenset({"class_specifier"}),
        type_nodes=frozenset({"struct_specifier", "enum_specifier", "type_definition"}),
        body_node_types=frozenset({"compound_statement"}),
        decorator_node=None,
        comment_prefix="//",
        uses_colon_after_signature=False,
    ),
}


# ─── Language detection ──────────────────────────────────────────────────────

# Lightweight pre-filter patterns for language detection. These are ONLY a
# quick check to avoid parsing with every language; actual detection is done
# by tree-sitter (fewest parse errors wins).
_LANGUAGE_PREFILTER: dict[CodeLanguage, list[re.Pattern[str]]] = {
    CodeLanguage.PYTHON: [
        re.compile(r"^\s*(def|class|import|from|async def)\s+\w+", re.MULTILINE),
        re.compile(r"^\s*@\w+", re.MULTILINE),
        re.compile(r'^\s*"""', re.MULTILINE),
        re.compile(r"^\s*if __name__\s*==", re.MULTILINE),
    ],
    CodeLanguage.JAVASCRIPT: [
        re.compile(r"^\s*(function|const|let|var|class|export)\s+\w+", re.MULTILINE),
        re.compile(r"^\s*async\s+(function|=>)", re.MULTILINE),
        re.compile(r"^\s*module\.exports", re.MULTILINE),
        re.compile(r"^\s*(import|export)\s+.*\s+from\s+['\"]", re.MULTILINE),
    ],
    CodeLanguage.TYPESCRIPT: [
        re.compile(r"^\s*(interface|type|enum|namespace)\s+\w+", re.MULTILINE),
        re.compile(r":\s*(string|number|boolean|any|void|Promise)\b", re.MULTILINE),
    ],
    CodeLanguage.GO: [
        re.compile(r"^\s*(func|type|package|import)\s+", re.MULTILINE),
        re.compile(r"^\s*func\s+\([^)]+\)\s+\w+", re.MULTILINE),
        re.compile(r"\bstruct\s*\{", re.MULTILINE),
    ],
    CodeLanguage.RUST: [
        re.compile(r"^\s*(fn|struct|enum|impl|mod|use|pub)\s+", re.MULTILINE),
        re.compile(r"^\s*#\[", re.MULTILINE),
    ],
    CodeLanguage.JAVA: [
        re.compile(r"^\s*(public|private|protected)\s+(class|interface|enum)", re.MULTILINE),
        re.compile(r"^\s*package\s+[\w.]+;", re.MULTILINE),
    ],
    CodeLanguage.C: [
        re.compile(r"^\s*#include\s*[<\"]", re.MULTILINE),
        re.compile(r"^\s*(int|void|char|float|double)\s+\w+\s*\(", re.MULTILINE),
        re.compile(r"^\s*typedef\s+", re.MULTILINE),
    ],
    CodeLanguage.CPP: [
        re.compile(r"^\s*#include\s*[<\"]", re.MULTILINE),
        re.compile(r"\bnamespace\s+\w+", re.MULTILINE),
        re.compile(r"::\w+", re.MULTILINE),
    ],
}


def _count_error_nodes(node: Any) -> int:
    """Count ERROR and MISSING nodes in a tree-sitter AST."""
    count = 0
    if node.type == "ERROR" or node.is_missing:
        count += 1
    for child in node.children:
        count += _count_error_nodes(child)
    return count


def detect_language(code: str) -> tuple[CodeLanguage, float]:
    """Detect the programming language of *code*.

    Uses tree-sitter AST parsing when available (most accurate), with a
    regex pre-filter to avoid parsing with all languages. Falls back to
    regex-only scoring when tree-sitter is unavailable.

    Returns:
        Tuple of (detected language, confidence score 0.0-1.0).
    """
    if not code or not code.strip():
        return CodeLanguage.UNKNOWN, 0.0

    sample = code[:5000]

    # Phase 1: Pre-filter — find candidate languages using quick regex
    candidates: dict[CodeLanguage, int] = {}
    for lang, patterns in _LANGUAGE_PREFILTER.items():
        score = 0
        for pattern in patterns:
            score += len(pattern.findall(sample))
        if score > 0:
            candidates[lang] = score

    if not candidates:
        return CodeLanguage.UNKNOWN, 0.0

    # Disambiguation: TypeScript superset of JavaScript
    if CodeLanguage.TYPESCRIPT in candidates and CodeLanguage.JAVASCRIPT in candidates:
        if candidates[CodeLanguage.TYPESCRIPT] >= 2:
            candidates[CodeLanguage.JAVASCRIPT] = 0

    # Disambiguation: C++ superset of C
    if CodeLanguage.CPP in candidates and CodeLanguage.C in candidates:
        if candidates[CodeLanguage.CPP] >= 2:
            candidates[CodeLanguage.C] = 0

    # Phase 2: If tree-sitter available, parse with candidates and pick fewest errors
    if _check_tree_sitter_available():
        best_lang = CodeLanguage.UNKNOWN
        min_errors = float("inf")
        best_node_count = 0
        code_bytes = bytes(code[:10000], "utf-8")

        # Sort candidates by pre-filter score (try most likely first)
        sorted_candidates = sorted(candidates.items(), key=lambda x: x[1], reverse=True)

        for lang, _prefilter_score in sorted_candidates:
            if lang == CodeLanguage.UNKNOWN or candidates.get(lang, 0) == 0:
                continue
            try:
                parser = _get_parser(lang.value)
                tree = parser.parse(code_bytes)
                error_count = _count_error_nodes(tree.root_node)
                node_count = tree.root_node.child_count

                # Prefer: fewest errors, then most top-level nodes (richer parse)
                if error_count < min_errors or (
                    error_count == min_errors and node_count > best_node_count
                ):
                    min_errors = error_count
                    best_lang = lang
                    best_node_count = node_count
            except (ValueError, ImportError):
                continue

        if best_lang != CodeLanguage.UNKNOWN:
            # Confidence based on error ratio
            total_lines = max(1, len(code.strip().split("\n")))
            error_ratio = min_errors / total_lines
            confidence = max(0.3, min(1.0, 1.0 - error_ratio))
            return best_lang, confidence

    # Phase 3: Fallback — regex-only scoring (no tree-sitter)
    best_lang = max(candidates, key=lambda k: candidates[k])
    best_score = candidates[best_lang]

    if best_score == 0:
        return CodeLanguage.UNKNOWN, 0.0

    confidence = min(1.0, 0.3 + (best_score * 0.1))
    return best_lang, confidence


# ─── Public dataclasses ──────────────────────────────────────────────────────


@dataclass
class CodeAwareConfig:
    """Configuration for AST-verified code compression.

    Attributes:
        docstring_mode: How to handle docstrings.
        target_compression_rate: Target compression ratio (0.2 = keep 20%).
        max_body_lines: Hard cap on kept lines per function body.
        min_chars: Minimum content size to attempt compression (below →
            passthrough; the ~25-token marker line would eat the savings).
        max_shippable_ratio: Renders at or above this char ratio pass
            through — marginal savings aren't worth the byte churn.
        min_shippable_ratio: Renders below this char ratio pass through —
            an implausibly tiny render signals an extraction bug.
        language_hint: Explicit language (None = auto-detect).
        semantic_analysis: Weight body budgets by symbol importance.
        enable_ccr: Persist originals for retrieval. False → nothing
            lossy ever ships (passthrough), because a truncating render
            without a recovery plane is silent loss.
    """

    docstring_mode: DocstringMode = DocstringMode.FIRST_LINE

    target_compression_rate: float = 0.2
    max_body_lines: int = 5

    min_chars: int = 400
    max_shippable_ratio: float = 0.9
    min_shippable_ratio: float = 0.05

    language_hint: str | None = None
    semantic_analysis: bool = True

    enable_ccr: bool = True


@dataclass
class CodeCompressionResult:
    """Result of code-aware compression.

    Attributes:
        compressed: The shipped render (marker included) — or the
            original bytes on any passthrough.
        original: Original code before compression.
        original_line_count: Line count before compression.
        compressed_line_count: Line count of the code render (marker
            line excluded).
        compression_ratio: Shipped chars / original chars (1.0 on
            passthrough).
        language: Detected or specified language.
        syntax_valid: Whether the shipped output parses.
        cache_key: CCR store key backing the marker (None on passthrough).
    """

    compressed: str
    original: str
    original_line_count: int
    compressed_line_count: int
    compression_ratio: float

    language: CodeLanguage = CodeLanguage.UNKNOWN

    syntax_valid: bool = True
    cache_key: str | None = None

    @property
    def lines_saved(self) -> int:
        """Number of source lines dropped by compression."""
        return max(0, self.original_line_count - self.compressed_line_count)


@dataclass
class CodeStructure:
    """Extracted structure from parsed code."""

    imports: list[str] = field(default_factory=list)
    type_definitions: list[str] = field(default_factory=list)
    class_definitions: list[str] = field(default_factory=list)
    function_signatures: list[str] = field(default_factory=list)
    top_level_code: list[str] = field(default_factory=list)


@dataclass
class _SymbolAnalysis:
    """Result of intra-file symbol importance analysis.

    All dicts are keyed by qualified name (e.g., 'ClassName.method')
    to avoid collisions between identically-named methods in different
    classes.
    """

    scores: dict[str, float] = field(default_factory=dict)
    calls: dict[str, set[str]] = field(default_factory=dict)
    ref_counts: dict[str, int] = field(default_factory=dict)
    body_line_counts: dict[str, int] = field(default_factory=dict)
    bare_names: dict[str, str] = field(default_factory=dict)  # qname -> short_name


# ─── Compressor ──────────────────────────────────────────────────────────────


class CodeAwareCompressor:
    """AST-preserving compression for source code.

    Parses code with tree-sitter, preserves imports / signatures / types /
    class scaffolding, and truncates function bodies whole-statement-at-a-
    time under a symbol-importance-weighted line budget. The shipped output
    is syntax-verified and CCR-backed; every failure mode resolves to
    passthrough (total function — ``compress`` never raises).

    Registry-shaped like the sibling compressors (TextCrusher et al.): a
    plain class the ``StrategyDispatcher`` calls ``compress()`` on. The
    message-level walking, protections, and gating all live in the router.
    """

    def __init__(self, config: CodeAwareConfig | None = None) -> None:
        """Initialize with *config* (defaults if None). Tree-sitter parsers
        are loaded lazily on first use."""
        self.config = config or CodeAwareConfig()

    # ─── Public API ─────────────────────────────────────────────────────

    def compress(
        self,
        content: str,
        language: str | None = None,
        context: str = "",
    ) -> CodeCompressionResult:
        """Compress source code, or pass it through untouched.

        Total: every failure mode (missing dep, unknown language, parse
        failure, invalid render, marginal savings, store failure) returns a
        passthrough result — never raises, never ships broken or
        unrecoverable output.

        Args:
            content: Source code to compress.
            language: Language name hint (e.g. ``"python"``); invalid or
                unknown hints fall back to auto-detection.
            context: Optional relevance context — symbols mentioned in it
                get a larger body budget.
        """
        if not content or not content.strip() or len(content) < self.config.min_chars:
            return self._passthrough(content)

        if not _check_tree_sitter_available():
            _warn_missing_dep_once()
            return self._passthrough(content)

        detected, _confidence = self._resolve_language(content, language)
        if detected is CodeLanguage.UNKNOWN:
            return self._passthrough(content)

        if not self.config.enable_ccr:
            # No recovery plane → a truncating render would be silent loss.
            return self._passthrough(content, detected)

        try:
            compressed_code = self._compress_with_ast(content, detected, context)
        except Exception as e:
            logger.warning("code_aware: AST compression failed (%s); passing through", e)
            return self._passthrough(content, detected)

        # Build the shipped render: code + Shape-H marker as a line comment.
        cache_key = hashlib.sha256(content.encode("utf-8", "surrogatepass")).hexdigest()[:24]
        original_lines = len(content.splitlines())
        compressed_lines = len(compressed_code.splitlines())
        marker = (
            f"{_LANG_CONFIGS[detected].comment_prefix} "
            f"[{original_lines} lines compressed to {compressed_lines}. "
            f"Retrieve more: hash={cache_key}]"
        )
        shipped = f"{compressed_code}\n{marker}"

        # Savings gates (cheap, before the verification parse).
        ratio = len(shipped) / max(len(content), 1)
        if ratio >= self.config.max_shippable_ratio:
            return self._passthrough(content, detected)
        if ratio < self.config.min_shippable_ratio:
            logger.warning(
                "code_aware: render implausibly small (ratio=%.3f) for %s; passing through",
                ratio,
                detected.value,
            )
            return self._passthrough(content, detected)

        # Syntax gate: the FINAL shipped bytes must parse cleanly.
        if not self._verify_syntax(shipped, detected):
            logger.warning(
                "code_aware: render failed syntax verification for %s (%d lines); passing through",
                detected.value,
                original_lines,
            )
            return self._passthrough(content, detected)

        # CCR gate: full original persisted under the marker's hash, or veto.
        if not self._persist_to_python_ccr(content, shipped, cache_key):
            return self._passthrough(content, detected)

        return CodeCompressionResult(
            compressed=shipped,
            original=content,
            original_line_count=original_lines,
            compressed_line_count=compressed_lines,
            compression_ratio=ratio,
            language=detected,
            syntax_valid=True,
            cache_key=cache_key,
        )

    # ─── Language resolution ────────────────────────────────────────────

    def _resolve_language(self, content: str, language: str | None) -> tuple[CodeLanguage, float]:
        """Resolve the language from hint → config → auto-detection.

        Total: a hint that isn't a known ``CodeLanguage`` value (code-fence
        tags like ``"bash"`` or ``"unknown"``) falls back to detection.
        """
        for hint in (language, self.config.language_hint):
            if not hint:
                continue
            try:
                candidate = CodeLanguage(hint.lower())
            except ValueError:
                continue
            if candidate is not CodeLanguage.UNKNOWN:
                return candidate, 1.0
        return detect_language(content)

    # ─── Passthrough + persistence ──────────────────────────────────────

    @staticmethod
    def _passthrough(
        content: str, language: CodeLanguage = CodeLanguage.UNKNOWN
    ) -> CodeCompressionResult:
        """Identity result: the original bytes ship, no marker, no store row."""
        n_lines = len(content.splitlines())
        return CodeCompressionResult(
            compressed=content,
            original=content,
            original_line_count=n_lines,
            compressed_line_count=n_lines,
            compression_ratio=1.0,
            language=language,
            syntax_valid=True,
            cache_key=None,
        )

    def _persist_to_python_ccr(self, original: str, shipped: str, cache_key: str) -> bool:
        """Persist *original* into the production ``CompressionStore`` under
        the marker's exact hash. Returns ``True`` on success, ``False`` on
        any failure (store import or store write) — the caller then serves
        the ORIGINAL uncompressed code so the marker never ships dangling.

        Thin delegator to the shared :func:`~._ccr_persist.persist_to_python_ccr`
        (ARCH-5; one implementation for the diff/log/search/text/code-aware
        family; ``compression_strategy`` = ``CompressionStrategy.CODE_AWARE.value``).
        Kept as a method for the test/monkeypatch seam."""
        return persist_to_python_ccr(
            original,
            shipped,
            cache_key,
            compression_strategy="code_aware",
            logger=logger,
        )

    # ─── Symbol importance analysis ─────────────────────────────────────

    def _analyze_symbol_importance(
        self,
        root: Any,
        code: str,
        language: CodeLanguage,
        context: str = "",
    ) -> _SymbolAnalysis:
        """Analyze symbol importance using distribution-based scoring.

        Collects raw signals (reference count, fan-out, visibility, context
        match, convention importance) per symbol, then normalizes with
        min-max scaling so scores are relative within the file. This adapts
        to any file structure: utility libraries, test files, orchestrators.
        """
        if not self.config.semantic_analysis:
            return _SymbolAnalysis()

        lang_config = _LANG_CONFIGS.get(language)
        if not lang_config:
            return _SymbolAnalysis()

        all_definition_types = lang_config.function_nodes | lang_config.class_nodes

        # Use qualified keys (ClassName.method) to avoid collisions
        definitions: dict[str, Any] = {}  # qualified_name -> node
        bare_names: dict[str, str] = {}  # qualified_name -> short_name
        all_identifiers: dict[str, int] = {}  # short_name -> count
        function_calls: dict[str, set[str]] = {}

        def collect_definitions(node: Any, parent_name: str = "") -> None:
            if node.type in all_definition_types:
                short_name = _get_definition_name(node)
                if short_name:
                    qualified = f"{parent_name}.{short_name}" if parent_name else short_name
                    definitions[qualified] = node
                    bare_names[qualified] = short_name
                    for child in node.children:
                        collect_definitions(child, parent_name=qualified)
                    return
            # Also check for decorated definitions
            if lang_config.decorator_node and node.type == lang_config.decorator_node:
                for child in node.children:
                    if child.type in all_definition_types:
                        short_name = _get_definition_name(child)
                        if short_name:
                            qualified = f"{parent_name}.{short_name}" if parent_name else short_name
                            definitions[qualified] = child
                            bare_names[qualified] = short_name
                            for grandchild in child.children:
                                collect_definitions(grandchild, parent_name=qualified)
                            return
            for child in node.children:
                collect_definitions(child, parent_name)

        def collect_identifiers(node: Any) -> None:
            if node.type in ("identifier", "property_identifier", "type_identifier"):
                text = node.text
                name = text.decode("utf-8") if isinstance(text, bytes) else str(text)
                all_identifiers[name] = all_identifiers.get(name, 0) + 1
            for child in node.children:
                collect_identifiers(child)

        def collect_calls_in_function(func_node: Any, func_qname: str) -> None:
            func_short = bare_names[func_qname]
            defined_short_names = set(bare_names.values())
            calls: set[str] = set()

            def walk(node: Any) -> None:
                if node.type in ("identifier", "property_identifier"):
                    text = node.text
                    name = text.decode("utf-8") if isinstance(text, bytes) else str(text)
                    if name in defined_short_names and name != func_short:
                        calls.add(name)
                for child in node.children:
                    walk(child)

            walk(func_node)
            function_calls[func_qname] = calls

        # Pass 1: Collect definitions with qualified names
        collect_definitions(root)

        if not definitions:
            return _SymbolAnalysis()

        # Pass 2: Collect all identifiers
        collect_identifiers(root)

        # Pass 3: Collect call relationships and body sizes
        body_line_counts: dict[str, int] = {}
        for qname, node in definitions.items():
            collect_calls_in_function(node, qname)
            node_text = code[node.start_byte : node.end_byte]
            body_line_counts[qname] = max(1, len(node_text.split("\n")) - 2)

        # Reference counts: subtract definition occurrences
        short_name_def_count: dict[str, int] = {}
        for short in bare_names.values():
            short_name_def_count[short] = short_name_def_count.get(short, 0) + 1

        ref_counts: dict[str, int] = {}
        for qname in definitions:
            short = bare_names[qname]
            count = all_identifiers.get(short, 0)
            ref_counts[qname] = max(0, count - short_name_def_count.get(short, 1))

        # Raw importance signals per symbol
        context_lower = context.lower() if context else ""
        context_words = set(re.split(r"[\s,;:.()\[\]{}\"']+", context_lower)) if context else set()
        context_words.discard("")

        raw_signals: dict[str, float] = {}
        for qname in definitions:
            short = bare_names[qname]
            refs = ref_counts.get(qname, 0)
            fan_out = len(function_calls.get(qname, set()))
            is_public = _is_public_symbol(short, language)

            raw = float(refs)
            raw += 1.0 if is_public else 0.0
            raw += fan_out * 0.5

            # Convention importance (language-specific)
            if language == CodeLanguage.PYTHON:
                if short.startswith("__") and short.endswith("__"):
                    raw += 2.0
            elif language == CodeLanguage.GO:
                if short and short[0].isupper():
                    raw += 1.0

            # Context boost
            if context_words:
                name_lower = short.lower()
                if name_lower in context_words or (
                    len(name_lower) > 3 and name_lower in context_lower
                ):
                    raw += 3.0

            raw_signals[qname] = raw

        # Normalize to 0-1 using min-max scaling
        values = list(raw_signals.values())
        min_val = min(values)
        max_val = max(values)
        range_val = max_val - min_val

        if range_val > 0:
            scores = {name: round((v - min_val) / range_val, 3) for name, v in raw_signals.items()}
        else:
            scores = dict.fromkeys(raw_signals, 0.5)

        return _SymbolAnalysis(
            scores=scores,
            calls=function_calls,
            ref_counts=ref_counts,
            body_line_counts=body_line_counts,
            bare_names=bare_names,
        )

    def _allocate_body_budget(self, analysis: _SymbolAnalysis, code: str) -> dict[str, int]:
        """Allocate the body line budget across functions using
        ``target_compression_rate``. Returns symbol name → max body lines."""
        if not analysis.scores or not analysis.body_line_counts:
            return {}

        scores = analysis.scores
        body_sizes = analysis.body_line_counts
        target_rate = self.config.target_compression_rate

        total_lines = len(code.strip().split("\n"))
        total_body_lines = sum(body_sizes.values())
        fixed_lines = max(0, total_lines - total_body_lines)

        target_total = total_lines * target_rate
        body_budget = max(0.0, target_total - fixed_lines)

        if total_body_lines == 0:
            return {}

        score_floor = 0.05

        weights: dict[str, float] = {}
        for name in scores:
            score = max(scores.get(name, 0.5), score_floor)
            size = body_sizes.get(name, 0)
            weights[name] = score * size

        total_weight = sum(weights.values())

        if total_weight == 0:
            per_func = max(0, int(body_budget / max(len(scores), 1)))
            return {name: min(per_func, body_sizes.get(name, 0)) for name in scores}

        limits: dict[str, int] = {}
        for qname in scores:
            allocation = body_budget * weights[qname] / total_weight
            max_lines = body_sizes.get(qname, 0)
            limit = min(int(round(allocation)), max_lines)
            limits[qname] = limit
            # Also store by short name so _get_body_limit can find it.
            short = analysis.bare_names.get(qname, qname)
            if short not in limits or limit > limits[short]:
                limits[short] = limit

        return limits

    # ─── Core compression ───────────────────────────────────────────────

    def _compress_with_ast(
        self,
        code: str,
        language: CodeLanguage,
        context: str,
    ) -> str:
        """Compress *code* using AST parsing with symbol importance analysis.

        Thread-safe: all mutable state is passed through parameters, not
        stored on self.

        Returns:
            The assembled compressed-code render.
        """
        parser = _get_parser(language.value)
        tree = parser.parse(bytes(code, "utf-8"))
        root = tree.root_node

        # Analyze symbol importance and allocate compression budget
        analysis = self._analyze_symbol_importance(root, code, language, context)
        body_limits = self._allocate_body_budget(analysis, code)

        # Extract structure using data-driven language config
        lang_config = _LANG_CONFIGS[language]
        structure = self._extract_structure(
            root, code, language, lang_config, body_limits, analysis
        )

        # Assemble compressed code
        return self._assemble_compressed(structure)

    # ─── Structure extraction (data-driven, all languages) ──────────────

    def _extract_structure(
        self,
        root: Any,
        code: str,
        language: CodeLanguage,
        lang_config: LangConfig,
        body_limits: dict[str, int],
        analysis: _SymbolAnalysis,
    ) -> CodeStructure:
        """Extract structure from the AST using the data-driven language
        config. A single visitor handles all languages by checking node
        types against the LangConfig tables."""
        structure = CodeStructure()
        captured_byte_ranges: list[tuple[int, int]] = []

        def visit(node: Any) -> None:
            node_type = node.type

            # Package declarations (Go, Java)
            if lang_config.package_node and node_type == lang_config.package_node:
                structure.imports.insert(0, _get_node_text(node, code))
                captured_byte_ranges.append((node.start_byte, node.end_byte))
                return

            # Import statements
            if node_type in lang_config.import_nodes:
                structure.imports.append(_get_node_text(node, code))
                captured_byte_ranges.append((node.start_byte, node.end_byte))
                return

            # Export statements (JS/TS) — may contain functions or re-exports
            if node_type == "export_statement":
                text = _get_node_text(node, code)
                # Check if this export wraps a function or class
                has_func_or_class = False
                for child in node.children:
                    if (
                        child.type in lang_config.function_nodes
                        or child.type in lang_config.class_nodes
                    ):
                        has_func_or_class = True
                        compressed = self._compress_function_ast(
                            child, code, language, lang_config, body_limits, analysis
                        )
                        # Reconstruct export with compressed inner definition
                        export_prefix = code[node.start_byte : child.start_byte]
                        export_suffix = code[child.end_byte : node.end_byte]
                        structure.function_signatures.append(
                            export_prefix + compressed + export_suffix
                        )
                        break
                if not has_func_or_class:
                    structure.imports.append(text)
                captured_byte_ranges.append((node.start_byte, node.end_byte))
                return

            # Decorated definitions (Python)
            if lang_config.decorator_node and node_type == lang_config.decorator_node:
                decorator_text = []
                definition_compressed = None
                for child in node.children:
                    if child.type == "decorator":
                        decorator_text.append(_get_node_text(child, code))
                    elif child.type in lang_config.function_nodes:
                        definition_compressed = self._compress_function_ast(
                            child, code, language, lang_config, body_limits, analysis
                        )
                    elif child.type in lang_config.class_nodes:
                        definition_compressed = self._compress_class_ast(
                            child, code, language, lang_config, body_limits, analysis
                        )
                if decorator_text and definition_compressed:
                    full_def = "\n".join(decorator_text) + "\n" + definition_compressed
                    # Route to correct list based on inner definition type
                    for child in node.children:
                        if child.type in lang_config.class_nodes:
                            structure.class_definitions.append(full_def)
                            break
                    else:
                        structure.function_signatures.append(full_def)
                elif definition_compressed:
                    structure.function_signatures.append(definition_compressed)
                captured_byte_ranges.append((node.start_byte, node.end_byte))
                return

            # Function/method definitions
            if node_type in lang_config.function_nodes:
                compressed = self._compress_function_ast(
                    node, code, language, lang_config, body_limits, analysis
                )
                structure.function_signatures.append(compressed)
                captured_byte_ranges.append((node.start_byte, node.end_byte))
                return

            # Class definitions — compress each method individually
            if node_type in lang_config.class_nodes:
                compressed = self._compress_class_ast(
                    node, code, language, lang_config, body_limits, analysis
                )
                structure.class_definitions.append(compressed)
                captured_byte_ranges.append((node.start_byte, node.end_byte))
                return

            # Type definitions
            if node_type in lang_config.type_nodes:
                structure.type_definitions.append(_get_node_text(node, code))
                captured_byte_ranges.append((node.start_byte, node.end_byte))
                return

            # Recurse into children
            for child in node.children:
                visit(child)

        visit(root)

        # Capture top-level code that wasn't handled by any of the above.
        # This preserves global variables, constants, if __name__ blocks,
        # module-level assignments, etc.
        for child in root.children:
            child_range = (child.start_byte, child.end_byte)
            if child_range not in captured_byte_ranges:
                text = _get_node_text(child, code).strip()
                if text:
                    structure.top_level_code.append(text)

        return structure

    # ─── Function/class compression (data-driven) ───────────────────────

    def _compress_function_ast(
        self,
        node: Any,
        code: str,
        language: CodeLanguage,
        lang_config: LangConfig,
        body_limits: dict[str, int],
        analysis: _SymbolAnalysis,
    ) -> str:
        """Compress a function/method using AST body detection.

        Uses the AST to find the body node directly instead of string-
        scanning for '{' or ':'. Works for all languages via
        ``lang_config.body_node_types``.

        Key insight: tree-sitter byte offsets may not include leading
        whitespace on the first line. LINE-based slicing from the original
        code preserves indentation faithfully (critical for methods inside
        classes).
        """
        code_lines = code.split("\n")
        start_row = node.start_point[0]
        end_row = node.end_point[0]
        node_lines = code_lines[start_row : end_row + 1]
        node_text = "\n".join(node_lines)

        func_name = _get_definition_name(node)
        body_limit = _get_body_limit(func_name, body_limits, self.config.max_body_lines)

        # Small enough to keep as-is
        if len(node_lines) <= body_limit + 2:
            return node_text

        # Find the body node using AST (not string scanning)
        body_node = None
        for child in node.children:
            if child.type in lang_config.body_node_types:
                body_node = child
                break

        if body_node is None:
            return node_text

        # Use line numbers to slice: this preserves original indentation.
        # tree-sitter gives 0-based row numbers.
        node_start_line = node.start_point[0]
        body_start_line = body_node.start_point[0]
        body_end_line = body_node.end_point[0]

        # Lines within the node (0-indexed relative to node start)
        sig_end = body_start_line - node_start_line  # exclusive
        body_end_rel = body_end_line - node_start_line + 1  # inclusive

        # Handle case where signature and body start on the SAME line
        # (common in brace languages: `function foo(arg) { ... }`)
        if sig_end == 0 and not lang_config.uses_colon_after_signature:
            # Signature and body on same line: keep them together — the
            # signature line includes the opening brace.
            first_line = node_lines[0]
            sig_with_brace = first_line.rstrip()
            signature_lines = [sig_with_brace]
            # Body lines are everything between { and } (inner content only)
            body_lines = node_lines[1:body_end_rel]
            after_lines = node_lines[body_end_rel:]
            _brace_in_signature = True
        else:
            signature_lines = node_lines[:sig_end]
            body_lines = node_lines[sig_end:body_end_rel]
            after_lines = node_lines[body_end_rel:]
            _brace_in_signature = False

        # For brace languages, detect opening/closing braces in the body lines.
        opening_brace_line = None
        closing_brace_line = None
        if not lang_config.uses_colon_after_signature:
            if _brace_in_signature:
                # Opening brace already in signature line — just find closing
                pass
            elif body_lines and body_lines[0].strip().startswith("{"):
                opening_brace_line = body_lines[0]
                body_lines = body_lines[1:]
            if body_lines and body_lines[-1].strip().endswith("}"):
                closing_brace_line = body_lines[-1]
                body_lines = body_lines[:-1]

        # Handle Python docstrings via AST
        docstring_text = ""
        ds_skip_lines = 0
        if language == CodeLanguage.PYTHON and body_node.child_count > 0:
            first_child = body_node.children[0]
            # tree-sitter Python may represent docstrings as:
            # - bare `string` node directly in block, OR
            # - `expression_statement` containing a `string` node
            ds_node = None
            if first_child.type == "string":
                ds_node = first_child
            elif first_child.type == "expression_statement" and first_child.child_count > 0:
                if first_child.children[0].type == "string":
                    ds_node = first_child

            if ds_node is not None:
                ds_lines_count = ds_node.end_point[0] - ds_node.start_point[0] + 1
                ds_start_rel = ds_node.start_point[0] - body_node.start_point[0]

                if self.config.docstring_mode == DocstringMode.FULL:
                    # Keep entire docstring as-is (preserve indentation)
                    docstring_text = "\n".join(
                        body_lines[ds_start_rel : ds_start_rel + ds_lines_count]
                    )
                elif self.config.docstring_mode == DocstringMode.FIRST_LINE:
                    # Use source lines directly (safe — preserves original quoting)
                    if ds_lines_count == 1:
                        # Single-line docstring: keep as-is
                        docstring_text = body_lines[ds_start_rel]
                    else:
                        # Multi-line docstring: keep first line, close it properly
                        first_ds_line = body_lines[ds_start_rel]
                        ds_indent = first_ds_line[
                            : len(first_ds_line) - len(first_ds_line.lstrip())
                        ]
                        stripped = first_ds_line.strip()

                        # Detect quote style from source
                        quote = '"""'
                        for q in ('r"""', "r'''", '"""', "'''"):
                            if stripped.startswith(q):
                                quote = q[-3:]
                                break

                        # Find where content starts (after opening quotes + prefix)
                        content_start = 0
                        for opener in ('r"""', "r'''", '"""', "'''"):
                            if stripped.startswith(opener):
                                content_start = len(opener)
                                break
                        first_content = stripped[content_start:].strip()

                        # Remove trailing closing quotes if the first line has them
                        for q in ('"""', "'''"):
                            if first_content.endswith(q):
                                first_content = first_content[: -len(q)].strip()

                        if first_content:
                            # """Some text here\n...\n"""  →  """Some text here"""
                            prefix_part = stripped[:content_start]
                            docstring_text = f"{ds_indent}{prefix_part}{first_content}{quote}"
                        else:
                            # Opening quote on its own line: """\n  text\n"""
                            if ds_start_rel + 1 < len(body_lines):
                                second_line = body_lines[ds_start_rel + 1].strip()
                                for q in ('"""', "'''"):
                                    if second_line.endswith(q):
                                        second_line = second_line[: -len(q)].strip()
                                if second_line:
                                    docstring_text = f"{ds_indent}{quote}{second_line}{quote}"
                                else:
                                    docstring_text = first_ds_line
                            else:
                                docstring_text = first_ds_line
                # elif REMOVE: docstring_text stays empty
                ds_skip_lines = ds_start_rel + ds_lines_count

        # --- Statement-based body truncation (never cuts mid-expression) ---
        #
        # Walk body_node.children (AST statements) instead of slicing lines.
        # Each child is a complete, syntactically valid statement. We keep
        # whole statements until the line budget is exhausted, so the output
        # always parses correctly.

        # Detect indentation from actual body code (preserves whatever the file uses)
        indent = _detect_indent(body_lines) if body_lines else "    "

        # Collect non-docstring body statements from the AST
        body_stmts: list[tuple[int, int]] = []  # (start_row, end_row) absolute
        ds_end_row = -1
        if ds_skip_lines > 0 and body_node.child_count > 0:
            # The docstring node occupies the first ds_skip_lines lines
            ds_end_row = body_node.start_point[0] + ds_skip_lines - 1

        # Punctuation tokens to skip (brace-language body delimiters, semicolons)
        _skip_types = frozenset({"{", "}", ";", ",", "comment", "line_comment", "block_comment"})

        for child in body_node.children:
            # Skip docstring node (already handled separately)
            if child.start_point[0] <= ds_end_row:
                continue
            # Skip punctuation and comment nodes
            if child.type in _skip_types:
                continue
            # Skip unnamed tokens (tree-sitter anonymous nodes like braces)
            if not child.is_named:
                continue
            body_stmts.append((child.start_point[0], child.end_point[0]))

        # Calculate lines per statement and keep whole statements until budget
        kept_lines: list[str] = []
        kept_line_count = 0
        stmts_kept = 0
        total_body_lines_count = sum(end - start + 1 for start, end in body_stmts)

        for start_row_stmt, end_row_stmt in body_stmts:
            stmt_lines = code_lines[start_row_stmt : end_row_stmt + 1]
            stmt_line_count = len(stmt_lines)

            # If adding this statement would exceed budget and we already have
            # at least one statement, stop here
            if kept_line_count + stmt_line_count > body_limit and stmts_kept > 0:
                break

            kept_lines.extend(stmt_lines)
            kept_line_count += stmt_line_count
            stmts_kept += 1

        omitted_lines = total_body_lines_count - kept_line_count

        # Build compressed output preserving original indentation
        result_parts: list[str] = []

        # Signature lines (may be multi-line)
        if signature_lines:
            result_parts.extend(signature_lines)
        else:
            sig_text = code[node.start_byte : body_node.start_byte].rstrip()
            result_parts.append(sig_text)

        if opening_brace_line is not None:
            result_parts.append(opening_brace_line)

        if docstring_text and self.config.docstring_mode is not DocstringMode.REMOVE:
            result_parts.append(docstring_text)

        if kept_lines:
            result_parts.extend(kept_lines)

        if omitted_lines > 0:
            result_parts.append(
                _make_omitted_comment(
                    func_name, omitted_lines, indent, lang_config.comment_prefix, analysis
                )
            )
            if lang_config.uses_colon_after_signature:
                result_parts.append(f"{indent}pass")

        if closing_brace_line is not None:
            result_parts.append(closing_brace_line)
        elif after_lines:
            result_parts.extend(after_lines)

        return "\n".join(result_parts)

    def _compress_class_ast(
        self,
        node: Any,
        code: str,
        language: CodeLanguage,
        lang_config: LangConfig,
        body_limits: dict[str, int],
        analysis: _SymbolAnalysis,
    ) -> str:
        """Compress a class by individually compressing each method.

        Preserves class-level attributes, type annotations, and decorators
        while compressing method bodies individually — each method's
        omitted-body comment lands at the correct indentation.
        """
        # Use line-based extraction to preserve indentation
        code_lines = code.split("\n")
        start_row = node.start_point[0]
        end_row = node.end_point[0]
        node_lines = code_lines[start_row : end_row + 1]
        node_text = "\n".join(node_lines)

        # Find the body node
        body_node = None
        for child in node.children:
            if child.type in lang_config.body_node_types:
                body_node = child
                break

        if body_node is None:
            return node_text

        # Class header (signature) — everything before the body
        node_start_line = node.start_point[0]
        body_start_line = body_node.start_point[0]
        sig_end = body_start_line - node_start_line
        header_lines = node_lines[:sig_end] if sig_end > 0 else [node_lines[0]]

        # Process each child of the class body individually
        body_parts: list[str] = []

        for child in body_node.children:
            # Use line-based extraction for children too
            child_start = child.start_point[0]
            child_end = child.end_point[0]
            child_text = "\n".join(code_lines[child_start : child_end + 1])

            # Methods/functions inside the class — compress individually
            if child.type in lang_config.function_nodes:
                compressed = self._compress_function_ast(
                    child, code, language, lang_config, body_limits, analysis
                )
                body_parts.append(compressed)
            # Decorated methods
            elif lang_config.decorator_node and child.type == lang_config.decorator_node:
                decorator_lines = []
                method_compressed = None
                for deco_child in child.children:
                    if deco_child.type == "decorator":
                        decorator_lines.append(_get_node_text(deco_child, code))
                    elif deco_child.type in lang_config.function_nodes:
                        method_compressed = self._compress_function_ast(
                            deco_child, code, language, lang_config, body_limits, analysis
                        )
                if decorator_lines and method_compressed:
                    body_parts.append("\n".join(decorator_lines) + "\n" + method_compressed)
                elif method_compressed:
                    body_parts.append(method_compressed)
                else:
                    body_parts.append(child_text)
            # Nested classes — recurse
            elif child.type in lang_config.class_nodes:
                compressed = self._compress_class_ast(
                    child, code, language, lang_config, body_limits, analysis
                )
                body_parts.append(compressed)
            else:
                # Class-level attributes, type annotations, docstrings, etc.
                # Keep them as-is with original indentation
                if child_text.strip():
                    body_parts.append(child_text)

        # Reconstruct class with proper indentation
        result_parts = list(header_lines)
        result_parts.extend(body_parts)

        # Handle closing brace for brace-delimited languages
        body_end_line = body_node.end_point[0]
        body_end_rel = body_end_line - node_start_line + 1
        after_lines = node_lines[body_end_rel:]
        if after_lines:
            result_parts.extend(after_lines)
        elif not lang_config.uses_colon_after_signature:
            # Ensure closing brace
            last_body_line = node_lines[-1] if node_lines else ""
            if last_body_line.strip() == "}":
                result_parts.append(last_body_line)

        return "\n".join(result_parts)

    @staticmethod
    def _assemble_compressed(structure: CodeStructure) -> str:
        """Assemble compressed code from the extracted structure."""
        parts: list[str] = []

        if structure.imports:
            parts.extend(structure.imports)
            parts.append("")

        if structure.type_definitions:
            parts.extend(structure.type_definitions)
            parts.append("")

        if structure.class_definitions:
            parts.extend(structure.class_definitions)
            parts.append("")

        if structure.function_signatures:
            parts.extend(structure.function_signatures)
            parts.append("")

        # Top-level code (global variables, constants, if __name__, etc.)
        if structure.top_level_code:
            parts.extend(structure.top_level_code)
            parts.append("")

        # Remove trailing empty lines
        while parts and not parts[-1].strip():
            parts.pop()

        return "\n".join(parts)

    @staticmethod
    def _verify_syntax(code: str, language: CodeLanguage) -> bool:
        """True iff *code* parses with zero ERROR and zero MISSING nodes."""
        try:
            parser = _get_parser(language.value)
            tree = parser.parse(bytes(code, "utf-8"))
            return not _has_syntax_issues(tree.root_node)
        except Exception:
            return False


# ─── Module-level helpers (stateless) ────────────────────────────────────────


def _get_node_text(node: Any, code: str) -> str:
    """Extract text from AST node."""
    return code[node.start_byte : node.end_byte]


def _get_definition_name(node: Any) -> str | None:
    """Extract the name identifier from a definition AST node."""
    for child in node.children:
        if child.type in ("identifier", "name", "type_identifier", "property_identifier"):
            text = child.text
            return text.decode("utf-8") if isinstance(text, bytes) else str(text)
    return None


def _is_public_symbol(name: str, language: CodeLanguage) -> bool:
    """Heuristic for whether a symbol is public/exported."""
    if not name:
        return False
    if language == CodeLanguage.GO:
        return name[0].isupper()
    return not name.startswith("_")


def _get_body_limit(
    func_name: str | None,
    body_limits: dict[str, int],
    max_body_lines: int,
) -> int:
    """Look up the allocated body line limit for a function.

    Falls back to ``max_body_lines`` if no budget allocation was computed;
    ``max_body_lines`` always acts as a hard cap.
    """
    if body_limits and func_name and func_name in body_limits:
        return min(body_limits[func_name], max_body_lines)
    return max_body_lines


def _make_omitted_comment(
    func_name: str | None,
    omitted_count: int,
    indent: str,
    comment_prefix: str,
    analysis: _SymbolAnalysis | None,
) -> str:
    """Build the omitted-body comment, with call info from the analysis."""
    calls_info = ""
    if analysis and func_name:
        for key in (
            func_name,
            *(k for k in analysis.calls if k.endswith(f".{func_name}")),
        ):
            if key in analysis.calls:
                called = analysis.calls[key]
                if called:
                    sorted_calls = sorted(called)[:5]
                    calls_info = "; calls: " + ", ".join(sorted_calls)
                    if len(called) > 5:
                        calls_info += f" +{len(called) - 5} more"
                break
    return f"{indent}{comment_prefix} [{omitted_count} lines omitted{calls_info}]"


def _detect_indent(lines: list[str]) -> str:
    """Detect the indentation used in a list of code lines."""
    for line in lines:
        if line.strip():
            return line[: len(line) - len(line.lstrip())]
    return "    "


def _has_syntax_issues(node: Any) -> bool:
    """Check if AST contains ERROR or MISSING nodes."""
    if node.type == "ERROR" or node.is_missing:
        return True
    for child in node.children:
        if _has_syntax_issues(child):
            return True
    return False


__all__ = [
    "CodeAwareCompressor",
    "CodeAwareConfig",
    "CodeCompressionResult",
    "CodeLanguage",
    "DocstringMode",
    "detect_language",
]
