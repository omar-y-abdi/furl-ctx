"""Guard the build-stage Rust selector without importing the native package."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_remote_image_selects_the_preinstalled_pinned_toolchain() -> None:
    config = (ROOT / "rust-toolchain.toml").read_text(encoding="utf-8")
    channels = re.findall(r'^channel\s*=\s*"([^"\n]+)"\s*$', config, re.MULTILINE)
    assert len(channels) == 1, "Expected one explicit repository toolchain channel"
    channel = re.escape(channels[0])
    text = (ROOT / "deploy/remote/Dockerfile").read_text(encoding="utf-8")
    text = text.replace("\\\n", " ")
    assert re.search(rf"^FROM rust:{channel}-bookworm AS rust$", text, re.MULTILINE)
    build = re.search(r"^FROM [^\n]+ AS build\n(.*?)(?=^FROM )", text, re.MULTILINE | re.DOTALL)
    assert build, "Remote Dockerfile must have a separate build stage"
    assert "maturin build" in build.group(1), "The build stage must compile the actual wheel"
    before_build = build.group(1).split("maturin build", 1)[0]
    env = "\n".join(line for line in before_build.splitlines() if line.startswith("ENV "))
    assert re.search(rf"\bRUSTUP_TOOLCHAIN={channel}(?:\s|$)", env), (
        "Select the preinstalled pinned toolchain explicitly so rustup does not "
        "auto-install rustfmt/clippy from rust-toolchain.toml during the wheel build"
    )
