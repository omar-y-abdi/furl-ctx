"""Vercel must discover an ASGI app and fail closed without durable configuration."""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from furl_remote.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def load_entrypoint():
    path = ROOT / "app.py"
    assert path.is_file(), "Vercel cannot discover the existing package-only entrypoint"
    spec = importlib.util.spec_from_file_location("vercel_entrypoint_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


@pytest.mark.asyncio
async def test_vercel_entrypoint_fails_closed_without_configuration(monkeypatch):
    for name in (
        "FURL_REMOTE_ORIGIN",
        "FURL_REMOTE_ISSUER",
        "FURL_REMOTE_JWKS_URL",
        "FURL_REMOTE_DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    app = load_entrypoint()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://remote.example.test"
    ) as client:
        for path in ("/healthz", "/readyz", "/mcp"):
            response = await client.get(path)
            assert response.status_code == 503
            assert response.headers["cache-control"] == "no-store"
            assert response.json() == {"error": "Service is not configured"}


def test_vercel_cannot_fall_back_to_ephemeral_sqlite(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("FURL_REMOTE_ORIGIN", "https://remote.example.test")
    monkeypatch.setenv("FURL_REMOTE_ISSUER", "https://issuer.example.test")
    monkeypatch.setenv("FURL_REMOTE_JWKS_URL", "https://issuer.example.test/jwks")
    monkeypatch.setenv("FURL_REMOTE_DATA_DIR", "/tmp/furl-would-lose-data")
    monkeypatch.delenv("FURL_REMOTE_DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="FURL_REMOTE_DATABASE_URL"):
        Settings.from_env()


def test_vercel_database_configuration_is_private_and_bounded(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("FURL_REMOTE_ORIGIN", "https://remote.example.test")
    monkeypatch.setenv("FURL_REMOTE_ISSUER", "https://issuer.example.test")
    monkeypatch.setenv("FURL_REMOTE_JWKS_URL", "https://issuer.example.test/jwks")
    monkeypatch.delenv("FURL_REMOTE_DATA_DIR", raising=False)
    url = "postgresql://furl:private-secret@database.example.test/furl?sslmode=verify-full"
    monkeypatch.setenv("FURL_REMOTE_DATABASE_URL", url)
    settings = Settings.from_env()
    assert settings.database_url == url
    assert "private-secret" not in repr(settings)
    assert settings.data_dir == Path("/tmp/furl-remote")
    assert settings.max_workers == 1
    assert settings.tenant_max_bytes == 64 * 1024 * 1024
    monkeypatch.setenv("FURL_REMOTE_DATABASE_URL", url.replace("verify-full", "disable"))
    with pytest.raises(ValueError, match="TLS"):
        Settings.from_env()


def test_vercel_runtime_configuration_targets_only_the_mcp_app():
    config = json.loads((ROOT / "vercel.json").read_text())
    assert config["functions"]["app.py"]["maxDuration"] >= 150
    assert (ROOT / ".python-version").read_text().strip() == "3.12"


def test_bundled_dependencies_are_available_to_isolated_child(tmp_path):
    """Vercel vendors dependencies outside the interpreter's site-packages."""
    import os
    import shutil
    import subprocess
    import sysconfig
    import venv

    bundle = tmp_path / "function"
    shutil.copytree(ROOT / "furl_remote", bundle / "furl_remote")
    vendor = bundle / "_vendor"
    shutil.copytree(
        sysconfig.get_path("purelib"),
        vendor,
        ignore=shutil.ignore_patterns("__pycache__", "mypy*", "ruff*", "pytest*", "_pytest*"),
    )
    python_dir = tmp_path / "bare-python"
    venv.EnvBuilder(with_pip=False).create(python_dir)
    script = """
import asyncio
import json
from pathlib import Path
from furl_remote.auth import Principal
from furl_remote.config import Settings
from furl_remote.workers import TenantWorkers
async def main():
    worker = TenantWorkers(Settings('https://furl.test', 'https://issuer.test', 'https://issuer.test/jwks', Path.cwd() / 'scratch'))
    worker.start()
    try:
        result = await worker.call(Principal('https://issuer.test', 'alice', frozenset()), 'furl_stats', {})
        assert not result.isError, result
        assert json.loads(result.content[0].text)["store"]["live_entries"] == 0, result
    finally:
        worker.close()
asyncio.run(main())
"""
    result = subprocess.run(
        [str(python_dir / "bin/python"), "-c", script],
        cwd=bundle,
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(bundle), str(vendor)))},
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr[-6000:]
