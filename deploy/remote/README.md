# Authenticated remote Furl MCP

This deployment exposes the actual Furl engine at `/mcp` using the official MCP
SDK's stateless Streamable HTTP transport. It is an **OAuth resource server**,
not an authorization server. It requires an existing OAuth 2.1 provider and
either PostgreSQL (Vercel) or a private persistent Linux filesystem (Docker).
Adding these files to GitHub does not provision an OAuth provider or database.
The static website in `site/` and this MCP service are separate Vercel projects.
The local CLI, stdio MCP server and private Secure MCP Tunnel remain unchanged.

## Architecture and boundaries

A verified access token's `(iss, sub)` pair selects a SHA-256-named workspace.
Every tool invocation runs the existing stdio server in a short-lived, bounded
subprocess using that workspace's SQLite database. The HTTP token and parent
secrets never enter the worker's environment. No process-global variable is
changed to switch users, and no request may choose its own tenant identifier.
Identical CCR hashes in different users' workspaces remain isolated.

All six public tools require OAuth. `furl_compress` and `furl_purge` require
`furl:write`; retrieve, search, list and stats require `furl:read`. Request both
scopes for the complete workflow. `file_path` and `furl_read` are deliberately
absent from the public API. Host attachments use `file` and `openai/fileParams`,
not a path on ChatGPT's filesystem or inline base64. Compression is conservatively
marked destructive because storing content can evict older entries at capacity.

For **Docker**, run one gateway process and one replica per persistent volume.
An exclusive volume lock rejects accidental double starts. Do not put these
live SQLite files on a shared network filesystem or use ephemeral storage.

For **Vercel**, PostgreSQL is the source of truth. A row lock serializes each
user's restore/run/save transaction across instances. The existing native worker
uses a private temporary SQLite copy; the gateway snapshots it using SQLite's
backup API (including committed WAL pages) and commits to PostgreSQL **before**
returning the tool result. Failed calls roll back. Temporary copies are removed
when the call exits. Database credentials are never passed to the child. This
avoids treating `/tmp` or a warm function instance as durable storage.

## Configure the real authorization provider

Following [OpenAI's authentication guide](https://developers.openai.com/plugins/build/auth),
configure an OAuth 2.1 authorization-code flow with **PKCE S256**. The provider
must offer usable authorization-server discovery and a ChatGPT-compatible client
registration mechanism (pre-registration, client metadata documents, or dynamic
registration). Register the exact redirect URLs displayed by ChatGPT, not a
wildcard. Configure the API audience/resource as the exact public URL ending
in `/mcp` and grant the two scopes above.

Access tokens must be asymmetrically signed **RS256 or ES256** JWTs containing
`iss`, `aud`, `sub`, `iat`, `exp` and a space-separated `scope` claim. An API key,
ID token for another audience, opaque bearer token or local tunnel key is not a
substitute. The gateway validates the signature, algorithm, issuer, audience,
expiry, not-before when present and per-tool scopes on every request. JWKS comes
only from an operator-pinned HTTPS URL, never `jku` or another token-supplied URL.
Cache lifetime is five minutes; unknown-key refreshes are throttled. Configure
short-lived access tokens: this deployment has no provider-specific introspection
or immediate revocation hook, so an already issued JWT can remain usable until
expiry (or signing-key removal plus cache expiry).

Set these environment variables to real values on the host. Do not commit tokens
or provider secrets:

| Variable | Meaning |
| --- | --- |
| `FURL_REMOTE_ORIGIN` | Public HTTPS origin, no path or trailing slash; port 443 |
| `FURL_REMOTE_ISSUER` | Exact issuer identifier, preserving any trailing slash |
| `FURL_REMOTE_JWKS_URL` | Exact provider HTTPS signing-key URL |
| `FURL_REMOTE_DATA_DIR` | Docker: required private persistent-volume directory. PostgreSQL mode: optional private scratch directory, default `/tmp/furl-remote` |
| `FURL_REMOTE_DATABASE_URL` | Required on Vercel: dedicated PostgreSQL service connection URL with TLS; never a public/anonymous key |
| `FURL_REMOTE_ATTACHMENT_HOSTS` | Optional comma-separated exact hosts or `*.domain` suffixes; default `chatgpt.com,*.oaiusercontent.com` |
| `OPENAI_APPS_CHALLENGE_TOKEN` | Optional exact token issued by the OpenAI submission portal |

An absent required variable stops the Docker gateway. The Vercel entrypoint
returns a non-cacheable 503 and logs the required variable **names**, not their
values. There is no anonymous or ephemeral-store fallback.
The attachment allowlist is enforced again after each redirect and is in addition
to the existing public-address checks. A wildcard does not include the domain
apex. Do not broaden this to arbitrary domains just to suppress a failed upload;
inspect the host's actual handoff contract and allow only trusted file hosts.

## Deploy on Vercel

Use the existing **remote** project with Root Directory **`deploy/remote`**, not
`site/` and not the repository root. Enable **Include source files outside of the
Root Directory**: the native crate and Python package live at the repository root.
The checked-in `vercel.json` selects Python, discovers `app.py`, runs
`install-vercel.sh` and allows 180 seconds per invocation. Python is pinned to
3.12. The install script builds the **current checkout** with the Rust toolchain
from `rust-toolchain.toml` and `maturin build --release --locked`, installs that
wheel with the pinned runtime dependencies, and primes tokenizer assets. It does
not replace current repository code with a PyPI release. Do not override the
install command with Docker or a website build.

Provision a dedicated PostgreSQL database/role through your chosen provider, then
apply the private schema once using that role:

```sh
psql "$FURL_REMOTE_DATABASE_URL" -v ON_ERROR_STOP=1 -f deploy/remote/migrations/001_postgres.sql
```

The gateway needs SELECT/INSERT/UPDATE/DELETE on `furl_remote.tenants` and USAGE on
its schema. Do not expose this schema through a browser-accessible database API.
Use a direct connection or a transaction-mode pooler, not statement pooling.
The driver uses transaction-scoped row locks, closes connections per call and
disables prepared statements. The service does not create tables during requests.

Set `FURL_REMOTE_ORIGIN`, `FURL_REMOTE_ISSUER`, `FURL_REMOTE_JWKS_URL` and
`FURL_REMOTE_DATABASE_URL` in the Vercel project's environment settings, then
redeploy. The database URL must explicitly enable TLS (`sslmode=require`,
`verify-ca` or `verify-full`); prefer provider-supported certificate verification.
The canonical origin must match the endpoint's actual Host header. Preview
hostname aliases are deliberately not implicitly trusted. Configure a preview's
own origin/database when testing it independently. A successful Vercel build
without these values is **not** a ready MCP service: it returns 503.

The canonical MCP domain must be accessible without a Vercel login/interstitial;
application access remains protected by OAuth. Confirm `/healthz` is 200,
`/readyz` is 200, and unauthenticated `/mcp` is 401 with resource metadata. Then
exercise compression followed by retrieval using a real issuer token. Vercel
READY alone does not prove database configuration, OAuth login or file handoff.

This adapter intentionally retains the existing engine instead of introducing
a second compression implementation. Its tradeoff is that the complete tenant
snapshot is transferred in both directions for each tool call, including reads
which update native store counters. Budget database transfer and function time
accordingly; this is bounded serverless hosting, not a high-throughput shared
storage engine. The snapshot cap is **64 MiB per tenant**, with **one worker per
function process**. The cap is enforced after execution as well as when restoring;
an over-budget write is rolled back and no retrieval receipt is returned. The
other request, attachment and worker limits below still apply. Connection and
commit overhead have a separate bounded allowance within the function deadline.

Expired snapshots are never restored. Idle rows are removed opportunistically
when allocating a tenant. For scheduled physical cleanup independent of traffic,
run this statement through the database provider's scheduler:

```sql
DELETE FROM furl_remote.tenants WHERE expires_at <= now();
```

Database backups/WAL have their own retention; neither TTL nor purge promises
secure erasure of those copies. PostgreSQL mode does not keep the Docker
workspace's per-process `session_stats.jsonl`; native persisted `store` counters
remain in the snapshot.

## Build and run with Docker

From the repository root on the chosen Linux Docker host:

```sh
docker build -f deploy/remote/Dockerfile -t furl-remote .
docker compose -f deploy/remote/compose.yaml up --build -d
```

Compose requires the three real URL variables above; the data path is provided
by its named volume. The container builds the current checkout's native wheel,
not an older PyPI release. It pins Python/Rust and Python runtime dependencies,
primes both tokenizer encodings, drops privileges to UID 10001, uses a read-only
root filesystem and keeps database files on `furl-data`. Initial named-volume
ownership is copied from the image. For an existing bind mount, explicitly set
ownership to `10001:10001` and directory permissions to `0700` before startup.
Back up the volume before upgrading; do not use `compose down --volumes` on data
you intend to retain. Pin the tested built image digest in your hosting system.

Terminate public TLS at the host's reverse proxy and forward only to the private
port 8000. Preserve the original **Host** header, reject unexpected hosts, disable
response caching, and allow at least 130 seconds for a tool call. Never log
Authorization headers, tool bodies or signed attachment URLs. Do not place the
endpoint behind an interactive bot challenge. Rate-limit at the edge and set a
volume quota appropriate to the host. This repository does not provision DNS,
certificates, paid hosting or an authorization-provider account.

For a non-container installation use Python 3.12 on Linux, install a wheel built
from this checkout with `maturin build --release --locked`, then:

```sh
python -m pip install -r deploy/remote/requirements.txt
# Install the wheel generated above, without changing the pinned runtime set.
python -m pip install --no-deps dist/furl_ctx-*.whl
PYTHONPATH=deploy/remote python -m furl_remote
```

`GET /healthz` reports gateway/storage initialization (and database schema/access
readiness in PostgreSQL mode). `GET /readyz` additionally
checks signing-key availability. Both require the configured Host header.
Neither claims an end-to-end successful user login or a writable CCR transaction.
Unauthenticated `/mcp` returns 401 with `WWW-Authenticate` linking to
`/.well-known/oauth-protected-resource/mcp`. That metadata points to the configured
external issuer. The root protected-resource metadata URL is an alias. The
external authorization server serves its own metadata; this gateway deliberately
does **not** invent a local `/.well-known/oauth-authorization-server` endpoint.

When `OPENAI_APPS_CHALLENGE_TOKEN` is set, the exact plain-text token is served at
`/.well-known/openai-apps-challenge` on the MCP host. Without it the route returns
404. Use that host's HTTPS origin as the portal's challenge base. Keep the token
value issued for this submission; the Google verification files in `site/` are
unrelated and are not overwritten.

## Limits, retention and failure semantics

Docker gateway limits are two active tool workers, 32 HTTP connections, 256
retained tenant workspaces, a 1 MiB JSON request/response ceiling and a 40 MiB
attachment ceiling. A worker has a 120-second call budget, 2 GiB address-space
limit and a 128 MiB per-file write limit. Busy requests fail rather than building
an unbounded process queue. Per-tenant calls are serialized; waiting for the
same user is limited to five seconds. Configure edge abuse protection as well;
these limits are not a distributed rate limiter.

The Docker tenant's **256 MiB preflight budget** blocks new compression when its current
workspace reaches that size; it is not a strict post-write quota. A call can
cross the budget. Inspect actual disk usage and enforce a host volume quota.
Read and purge operations remain possible after the preflight budget is reached.
Underlying CCR entry limits and eviction still apply. All workers use the stable
`furl-remote-v1` store namespace inside their isolated directories so that a
volume can move to another mount path without changing database names.

The default CCR TTL is **24 hours**. Stale entries are not retrievable. Idle tenant
Docker workspaces are removed after 24 hours of inactivity, checked at startup and every
five minutes while the gateway runs. Active workspaces can retain expired pages
inside SQLite until normal cleanup or maintenance; this is not a guarantee of
physical secure erasure. Purge removes the requested CCR entries, not filesystem
backups. Restarting the gateway retains live entries on the same volume.
Disconnecting ChatGPT does not purge data. The operator must disclose and implement
backup retention, account deletion, storage maintenance and any legal retention.

A failed storage open is rejected, not reported as an empty store. A compressed
result marked volatile is not forwarded as a durable hash: a worker about to exit
cannot promise retrieval from its memory. A timeout or lost connection can occur
**after** a write committed. Inspect stored entries before retrying; there is no
cross-request idempotency-key protocol. Use narrow retrieval slices rather than
requesting entire large originals. `furl_stats`'s `store` block is persisted;
top-level process counters describe only the short-lived worker.

## Verification and release gate

```sh
PYTHONPATH=deploy/remote pytest deploy/remote/tests tests/test_mcp_remote_metadata.py
mypy deploy/remote/furl_remote deploy/remote/app.py --ignore-missing-imports
```

Tests use a test-only issuer with real RSA signatures and a mocked JWKS transport;
HTTP MCP requests then invoke the real stdio Furl engine. They cover wrong tokens,
scopes, bounded requests/JWKS, metadata, same-hash cross-user isolation, concurrent
users, all six tools, purge, gateway restart and real SQLite-open failures. They
do not prove that an external OAuth provider or ChatGPT attachment handoff is live.
CI also builds the container and checks startup, metadata and fail-closed auth.
For the PostgreSQL tests, install `requirements-vercel.txt` and set
`FURL_TEST_DATABASE_URL` to a **disposable** local PostgreSQL database. These tests
create/truncate the test schema. CI supplies PostgreSQL 16 and runs cold-instance
retrieval, cross-tenant isolation, all six tools, concurrent writes, quota races,
expiry, corrupt storage, worker cancellation, over-budget rollback and deferred
COMMIT rejection. A 33 MiB staged-file test covers native compression and cold
retrieval, without claiming the external attachment URL handoff was exercised. A separate
bare-interpreter test reproduces Vercel's vendored dependency layout so the child
must find its native engine without relying on global site-packages.

Before any Directory submission, verify the actual HTTPS endpoint using two real
accounts, token refresh, account disconnect/reconnect, expiry, each of the six
tools, the target 33 MB ChatGPT attachment, restart and deletion. Publish a hosted
service privacy notice identifying the real operator, OAuth provider, host,
locations, subprocessors, retention and backups. The website's current notice
covers the static site and local usage, not a service that has not been launched.
Add the actual domain-verification token and reviewer-accessible credentials/demo.
Do not claim an approved, publicly available or fully production-verified plugin
based only on local tests. See [OpenAI submission requirements](https://developers.openai.com/plugins/deploy/submission).
