"""Contract tests for the installable Codex plugin package."""

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_CLAUDE_PLUGIN_ROOT = _ROOT / "plugins" / "furl"
_PLUGIN_ROOT = _ROOT / "plugins" / "furl-codex"
_MANIFEST_PATH = _PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
_MARKETPLACE_PATH = _ROOT / ".agents" / "plugins" / "marketplace.json"

_MANIFEST_FIELDS = {
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "skills",
    "mcpServers",
    "interface",
}
_INTERFACE_FIELDS = {
    "displayName",
    "shortDescription",
    "longDescription",
    "developerName",
    "category",
    "capabilities",
    "websiteURL",
    "defaultPrompt",
    "brandColor",
    "screenshots",
}


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_codex_manifest_uses_supported_components_only() -> None:
    manifest = _load(_MANIFEST_PATH)

    assert set(manifest) == _MANIFEST_FIELDS
    assert manifest["name"] == "furl"
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert "hooks" not in manifest
    assert (_PLUGIN_ROOT / manifest["skills"]).is_dir()
    assert (_PLUGIN_ROOT / manifest["mcpServers"]).is_file()
    # Codex default-loads its hook manifest when no override is present; keep its package root separate from Claude's hook bundle.
    assert not (_PLUGIN_ROOT / "hooks" / "hooks.json").exists()

    interface = manifest["interface"]
    assert isinstance(interface, dict)
    assert set(interface) == _INTERFACE_FIELDS
    assert interface["displayName"] == "Furl"
    assert interface["capabilities"] == ["Interactive"]
    assert interface["defaultPrompt"]


def test_codex_marketplace_points_to_furl_plugin() -> None:
    marketplace = _load(_MARKETPLACE_PATH)

    assert marketplace["name"] == "furl"
    assert marketplace["interface"] == {"displayName": "Furl"}
    assert marketplace["plugins"] == [
        {
            "name": "furl",
            "source": {"source": "local", "path": "./plugins/furl-codex"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Productivity",
        }
    ]
    assert (_ROOT / marketplace["plugins"][0]["source"]["path"]).resolve() == _PLUGIN_ROOT


def test_codex_and_claude_manifests_share_identity() -> None:
    codex = _load(_MANIFEST_PATH)
    claude = _load(_CLAUDE_PLUGIN_ROOT / ".claude-plugin" / "plugin.json")

    for field in ("name", "homepage", "license"):
        assert codex[field] == claude[field]


def test_codex_and_claude_packages_share_mcp_and_skill_content() -> None:
    for relative_path in (Path(".mcp.json"), Path("skills/furl/SKILL.md")):
        assert (_PLUGIN_ROOT / relative_path).read_bytes() == (
            _CLAUDE_PLUGIN_ROOT / relative_path
        ).read_bytes()
