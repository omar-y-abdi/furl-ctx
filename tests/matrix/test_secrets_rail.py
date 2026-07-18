"""MATRIX · secrets rail — the DOCUMENTED redaction contract, across families.

The contract discovered from source and the existing redactor suite
(``furl_ctx/compress.py:141-156``; ``tests/test_b3_redact_purge.py``) is NOT "a
secret is always scrubbed". It is OPT-IN and FAIL-CLOSED:

* ``CompressConfig.redactor`` defaults to ``None`` — with no redactor, behavior is
  "byte-identical to today" (``compress.py:562``): the secret SURVIVES into the
  compressed output and the store, and ``retrieve`` returns it verbatim. This is
  required by the byte-exact promise itself and is pinned below as the default.
* When a redactor IS configured it runs BEFORE compression's fail-open boundary,
  so downstream compression/offload/store only ever see redacted content, and a
  later ``retrieve`` returns the REDACTED original (``compress.py:153-156``).
* If the redactor RAISES, ``compress`` RAISES (fail-closed): nothing is compressed,
  offloaded, stored, or returned — "no output rather than a leak".

``test_b3_redact_purge.py`` proves this on a JSON envelope. This register EXTENDS
it across the new content families (each routing through a different compressor),
with API-key / token / AWS-id / PEM-block / password shapes — proving the
pre-routing scrub holds regardless of family or secret shape. All secret literals
are assembled from parts (scanner-hygiene).
"""

from __future__ import annotations

import pytest

from furl_ctx import CompressConfig, compress
from tests.matrix import _matrix as m


@pytest.fixture(autouse=True)
def _builtins_off(monkeypatch):
    """This rail pins the CONFIGURED-redactor contract and the byte-exact OPT-OUT
    path across content families. Several secret shapes here (``sk-``, ``ghp_``,
    ``AKIA``, PEM) would ALSO be caught by the ON-by-default built-in credential
    redactor (audit Crit-4), scrubbing them to a different marker before the
    configured redactor runs. Opt the built-ins out so each family exercises
    exactly the configured redactor / byte-exact-default it was written for; the
    default-on built-ins are pinned in test_redaction_env.py."""
    monkeypatch.setenv("FURL_REDACT_BUILTINS", "0")


# (family_id, generator, secret_builder) — rotate the secret SHAPE across families
# so the rail is exercised for API keys, tokens, AWS ids, PEM blocks, passwords.
_FAMILY_SECRETS = [
    ("yaml", m.yaml_document, m.fake_openai_key),
    ("xml", m.xml_document, m.fake_github_token),
    ("sql", m.sql_dump, m.fake_aws_key_id),
    ("go", m.go_source, m.fake_pem_block),
    ("java", m.java_source, m.fake_password_kv),
    ("ansi_log", m.ansi_log, m.fake_openai_key),
    ("crlf_log", m.crlf_log, m.fake_github_token),
]


@pytest.mark.parametrize(
    "family_id, generator, secret_builder",
    _FAMILY_SECRETS,
    ids=[c[0] for c in _FAMILY_SECRETS],
)
def test_configured_redactor_scrubs_secret_before_store(
    family_id, generator, secret_builder, salt
) -> None:
    secret = secret_builder()
    content = m.salted(generator(secret=secret), salt)
    assert secret in content, "precondition: the secret is actually embedded"

    result = compress(
        [{"role": "tool", "content": content}],
        model="gpt-4o",
        config=CompressConfig(redactor=m.scrubbing_redactor(secret)),
    )

    out = result.messages[0]["content"]
    assert result.error is None
    assert secret not in out, f"{family_id}: secret leaked into the compressed output"
    assert not m.store_contains(secret), f"{family_id}: secret persisted in the CCR store"

    if result.ccr_hashes:
        # Offloaded: the stored (retrievable) original is the REDACTED one.
        redacted = content.replace(secret, "[REDACTED]")
        recovered = m.retrieve(result.ccr_hashes[0])
        assert recovered is not None, f"{family_id}: offload pointer dangles"
        assert secret not in recovered, f"{family_id}: secret recoverable via retrieve()"
        assert recovered == redacted, f"{family_id}: stored original is not the redacted content"


def test_raising_redactor_fails_closed_on_non_json_family(salt) -> None:
    # Generalize the fail-closed guarantee to a NON-JSON family (B3 pins JSON): a
    # redactor that raises must propagate out of compress() and leave nothing
    # stored — no output rather than a leak.
    secret = m.fake_openai_key()
    content = m.salted(m.yaml_document(secret=secret), salt)

    def _boom(_raw: str) -> str:
        raise RuntimeError("redactor exploded")

    with pytest.raises(RuntimeError, match="redactor exploded"):
        compress(
            [{"role": "tool", "content": content}],
            model="gpt-4o",
            config=CompressConfig(redactor=_boom),
        )
    assert not m.store_contains(secret), "fail-closed: nothing may reach the store on a raise"


def test_optout_no_redactor_lets_secret_survive_byte_exact(salt) -> None:
    """Pin the byte-exact OPT-OUT path: with the built-in credential redactor
    disabled (``FURL_REDACT_BUILTINS=0``, set by the autouse fixture) and no
    configured redactor, there is NO scrubbing — the secret is preserved
    byte-exact through compression and offload, and ``retrieve`` returns it
    verbatim. This is the escape hatch for callers who need the raw bytes; the
    ON-by-default built-in redaction is pinned in test_redaction_env.py.
    """
    secret = m.fake_openai_key()
    content = m.salted(m.yaml_document(secret=secret), salt)
    result = compress([{"role": "tool", "content": content}], model="gpt-4o")
    assert result.error is None
    assert result.ccr_hashes, "fixture is expected to offload"
    recovered = m.retrieve(result.ccr_hashes[0])
    assert recovered == content, "default path must store the raw original byte-exact"
    assert secret in recovered, "with no redactor the secret survives by design"
