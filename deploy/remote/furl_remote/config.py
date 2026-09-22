"""Explicit deployment configuration; no insecure auth or ephemeral-store defaults."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .auth import https_url


@dataclass(frozen=True)
class Settings:
    origin: str
    issuer: str
    jwks_url: str
    data_dir: Path
    read_scope: str = "furl:read"
    write_scope: str = "furl:write"
    max_workers: int = 2
    max_tenants: int = 256
    tenant_max_bytes: int = 256 * 1024 * 1024
    ttl_seconds: int = 86400
    call_timeout: float = 120.0
    attachment_hosts: str = "*.oaiusercontent.com"
    challenge_token: str | None = None

    def __post_init__(self) -> None:
        for value in (self.origin, self.issuer, self.jwks_url):
            https_url(value)
        if urlsplit(self.origin).path:
            raise ValueError("FURL_REMOTE_ORIGIN must be an HTTPS origin without a trailing slash")
        if not self.data_dir.is_absolute():
            raise ValueError("FURL_REMOTE_DATA_DIR must be an absolute persistent-volume path")
        if (
            self.data_dir == Path("/")
            or self.data_dir.is_symlink()
            or self.data_dir.resolve() != self.data_dir
        ):
            raise ValueError("Use a dedicated data directory without symbolic links")
        if not 1 <= self.max_workers <= 8 or not 1 <= self.max_tenants <= 10000:
            raise ValueError("Worker/tenant limits are outside supported bounds")
        if not 1 <= self.ttl_seconds <= 604800 or not 1 <= self.call_timeout <= 300:
            raise ValueError("Retention/timeout is outside supported bounds")
        if not 1048576 <= self.tenant_max_bytes <= 1073741824:
            raise ValueError("Tenant storage budget must be 1 MiB to 1 GiB")
        if not self.attachment_hosts.strip():
            raise ValueError("An attachment-host allowlist is required")
        if self.challenge_token is not None and not re.fullmatch(
            r"[A-Za-z0-9_.=-]{1,512}", self.challenge_token
        ):
            raise ValueError("Set only the exact domain-verification token")
        for scope in (self.read_scope, self.write_scope):
            if not scope or any(c.isspace() or c in '\\"' for c in scope):
                raise ValueError("Configure one OAuth scope identifier per permission")

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            origin=os.environ["FURL_REMOTE_ORIGIN"],
            issuer=os.environ["FURL_REMOTE_ISSUER"],
            jwks_url=os.environ["FURL_REMOTE_JWKS_URL"],
            data_dir=Path(os.environ["FURL_REMOTE_DATA_DIR"]),
            attachment_hosts=os.environ.get("FURL_REMOTE_ATTACHMENT_HOSTS", "*.oaiusercontent.com"),
            challenge_token=os.environ.get("OPENAI_APPS_CHALLENGE_TOKEN") or None,
        )
