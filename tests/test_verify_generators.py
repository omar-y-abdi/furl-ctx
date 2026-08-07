"""Regression tests for verification-harness inputs."""

from verify.generators import gen_opaque


def test_opaque_generator_source_capture_is_committed() -> None:
    case = gen_opaque(seed=1000, n=7, tier="low")

    assert len(case.items) == 7
