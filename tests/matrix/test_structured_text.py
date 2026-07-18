"""MATRIX · structured-text families with ZERO prior coverage: YAML · XML · SQL · TOML.

Contract under test (``_matrix.assert_text_lossless_byte_exact``): each is routed
to a byte-exact recovery path — passthrough OR whole-content offload whose
surfaced ``<<ccr:HASH>>`` pointer's ``retrieve(hash)`` reconstructs the original
BYTE-EXACT. No silent loss. These three content types are called out in the scout
gap inventory (§4.2/§4.3/§4.4) as having no test exercise anywhere; the engine
stores the raw original on the text route (``code_aware``/``csv``/``envelope``
persist ``content`` verbatim), so recovery is byte-faithful — pinned here.

Every test threads a per-test ``salt`` (see conftest) so its compress is a genuine
cold pass, immune to the process-global cache carryover documented in
``test_result_cache_divergence``.
"""

from __future__ import annotations

from tests.matrix import _matrix as m


def test_yaml_document_is_byte_exact_recoverable(salt) -> None:
    m.assert_text_lossless_byte_exact(m.yaml_document(), salt=salt)


def test_xml_document_is_byte_exact_recoverable(salt) -> None:
    m.assert_text_lossless_byte_exact(m.xml_document(), salt=salt)


def test_sql_dump_is_byte_exact_recoverable(salt) -> None:
    m.assert_text_lossless_byte_exact(m.sql_dump(), salt=salt)


def test_yaml_actually_offloads_and_round_trips_whole(salt) -> None:
    # Stronger, path-pinning variant: this size DOES offload (not passthrough),
    # so a single surfaced hash must reconstruct the whole document byte-exact.
    doc = m.salted(m.yaml_document(), salt)
    result = m.run(doc)
    assert result.ccr_hashes, "yaml at this size is expected to offload"
    assert m.retrieve(result.ccr_hashes[0]) == doc


def test_xml_whole_offload_preserves_declaration_and_closing_tag(salt) -> None:
    doc = m.salted(m.xml_document(), salt)
    result = m.run(doc)
    assert result.ccr_hashes, "xml at this size is expected to offload"
    recovered = m.retrieve(result.ccr_hashes[0])
    assert recovered == doc
    # Spot-pin the fragile edges that a re-serializer would drop.
    assert recovered.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "</audit>" in recovered


def test_sql_dump_preserves_statement_terminators(salt) -> None:
    doc = m.salted(m.sql_dump(), salt)
    result = m.run(doc)
    assert result.ccr_hashes, "sql at this size is expected to offload"
    recovered = m.retrieve(result.ccr_hashes[0])
    assert recovered == doc
    assert recovered.count(";") == doc.count(";")  # every ';' terminator survives


def test_toml_document_is_byte_exact_recoverable(salt) -> None:
    m.assert_text_lossless_byte_exact(m.toml_document(), salt=salt)


def test_toml_actually_offloads_and_round_trips_whole(salt) -> None:
    doc = m.salted(m.toml_document(), salt)
    result = m.run(doc)
    assert result.ccr_hashes, "toml at this size is expected to offload"
    assert m.retrieve(result.ccr_hashes[0]) == doc


def test_toml_whole_offload_preserves_table_headers_and_arrays(salt) -> None:
    doc = m.salted(m.toml_document(), salt)
    result = m.run(doc)
    assert result.ccr_hashes, "toml at this size is expected to offload"
    recovered = m.retrieve(result.ccr_hashes[0])
    assert recovered == doc
    # Spot-pin the structural edges a re-serializer would drop: every table
    # header and the inline arrays survive verbatim.
    assert recovered.count("[service.svc_") == 120
    assert 'tags = ["env-0", "tier-0"]' in recovered
