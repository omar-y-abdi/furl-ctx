"""Validate access tokens against an operator-pinned OAuth issuer and JWKS URL."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx
import jwt


class TokenRejected(ValueError):
    pass


class IssuerUnavailable(RuntimeError):
    pass


def https_url(value: str) -> str:
    if any(ord(c) <= 32 for c in value):
        raise ValueError(
            "HTTPS configuration URLs must not contain whitespace or control characters"
        )
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Configure an absolute HTTPS URL without credentials, query or fragment")
    return value


@dataclass(frozen=True)
class Principal:
    issuer: str
    subject: str
    scopes: frozenset[str]

    @property
    def tenant_key(self) -> str:
        identity = json.dumps([self.issuer, self.subject], separators=(",", ":"))
        return hashlib.sha256(identity.encode()).hexdigest()


class OAuthVerifier:
    def __init__(
        self, issuer: str, jwks_url: str, audience: str, client: httpx.AsyncClient
    ) -> None:
        self.issuer = https_url(issuer)
        self.jwks_url = https_url(jwks_url)
        self.audience = https_url(audience)
        self.client = client
        self._keys: dict[str, jwt.PyJWK] = {}
        self._expires = 0.0
        self._last_fetch = -float("inf")
        self._lock = asyncio.Lock()

    async def _key(self, kid: str) -> jwt.PyJWK:
        async with self._lock:
            now = time.monotonic()
            stale = now >= self._expires
            if stale or kid not in self._keys:
                # Random kid values must not turn every invalid token into an issuer request.
                if now - self._last_fetch < 10:
                    if stale:
                        raise IssuerUnavailable("Signing keys unavailable")
                    raise TokenRejected("Unknown signing key")
                self._last_fetch = now
                try:
                    with anyio.fail_after(10):
                        async with self.client.stream(
                            "GET", self.jwks_url, timeout=10, follow_redirects=False
                        ) as response:
                            if response.status_code != 200:
                                raise IssuerUnavailable("Signing keys unavailable")
                            raw = bytearray()
                            async for chunk in response.aiter_bytes():
                                if len(raw) + len(chunk) > 65536:
                                    raise IssuerUnavailable("Signing key document exceeds limit")
                                raw.extend(chunk)
                    doc = json.loads(raw)
                    items = doc.get("keys")
                    if not isinstance(items, list) or not 1 <= len(items) <= 32:
                        raise IssuerUnavailable("Invalid signing key document")
                    keys: dict[str, jwt.PyJWK] = {}
                    for item in items:
                        if not isinstance(item, dict) or item.get("use", "sig") != "sig":
                            continue
                        key_id = item.get("kid")
                        if not isinstance(key_id, str) or not key_id or key_id in keys:
                            raise IssuerUnavailable("Invalid signing key identifiers")
                        if item.get("kty") not in ("RSA", "EC"):
                            continue
                        algorithm = item.get("alg") or (
                            "RS256" if item["kty"] == "RSA" else "ES256"
                        )
                        if algorithm not in ("RS256", "ES256"):
                            continue
                        keys[key_id] = jwt.PyJWK.from_dict(item, algorithm=algorithm)
                    if not keys:
                        raise IssuerUnavailable("No supported signing keys")
                    self._keys, self._expires = keys, now + 300
                except (
                    TimeoutError,
                    httpx.HTTPError,
                    ValueError,
                    TypeError,
                    KeyError,
                    AttributeError,
                    jwt.PyJWTError,
                ) as exc:
                    raise IssuerUnavailable("Signing keys unavailable") from exc
            if kid not in self._keys:
                raise TokenRejected("Unknown signing key")
            return self._keys[kid]

    async def ready(self) -> None:
        # Refresh expired metadata without fetching an attacker-selected URL.
        try:
            await self._key(next(iter(self._keys), "__readiness__"))
        except TokenRejected:
            if not self._keys:
                raise IssuerUnavailable("Signing keys unavailable") from None

    async def verify(self, token: str) -> Principal:
        if not token or len(token) > 16384:
            raise TokenRejected("Invalid access token")
        try:
            header = jwt.get_unverified_header(token)
            kid, algorithm = header.get("kid"), header.get("alg")
            if not isinstance(kid, str) or not kid or len(kid) > 256:
                raise TokenRejected("Missing signing key identifier")
            if algorithm not in ("RS256", "ES256"):
                raise TokenRejected("Unsupported signature algorithm")
            key = await self._key(kid)
            if key.algorithm_name != algorithm:
                raise TokenRejected("Signature algorithm mismatch")
            claims: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=[algorithm],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["iss", "aud", "sub", "iat", "exp"]},
            )
            subject, scope = claims["sub"], claims.get("scope", "")
            if not isinstance(subject, str) or not subject.strip() or len(subject) > 1024:
                raise TokenRejected("Invalid subject")
            if not isinstance(scope, str) or len(scope) > 4096:
                raise TokenRejected("Invalid scope")
            # PyJWT accepts some numeric strings; enforce the access-token claim types here.
            if any(type(claims[k]) not in (int, float) for k in ("iat", "exp")):
                raise TokenRejected("Invalid token timestamps")
            return Principal(self.issuer, subject, frozenset(scope.split()))
        except (jwt.PyJWTError, ValueError, TypeError, OverflowError) as exc:
            raise TokenRejected("Invalid access token") from exc
