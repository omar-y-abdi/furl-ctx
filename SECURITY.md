# Security Policy

## Supported Versions

| Version           | Supported          |
| ----------------- | ------------------ |
| latest release    | :white_check_mark: |
| older releases    | :x:                |

Only the latest published release receives security fixes — upgrade to
the newest version before reporting.

## Reporting a Vulnerability

We take security vulnerabilities seriously. If you discover a security issue, please report it responsibly.

### How to Report

**Please DO NOT open a public GitHub issue for security vulnerabilities.**

Instead, report privately via **GitHub Security Advisories**: open
[github.com/omar-y-abdi/furl/security/advisories](https://github.com/omar-y-abdi/furl/security/advisories)
and click **"Report a vulnerability"**.

Include the following information:
- Type of vulnerability (e.g., injection, data exposure, authentication bypass)
- Full path of the affected source file(s)
- Step-by-step instructions to reproduce the issue
- Proof-of-concept or exploit code (if possible)
- Impact assessment

### What to Expect

1. **Acknowledgment**: We will acknowledge receipt within 48 hours
2. **Assessment**: We will assess the vulnerability and determine its severity
3. **Updates**: We will keep you informed of our progress
4. **Resolution**: We aim to resolve critical issues within 7 days
5. **Credit**: With your permission, we will credit you in the security advisory

### Security Best Practices for Users

When using Furl:

1. **API Keys**: Never commit API keys. Use environment variables.
2. **Log Files**: Be aware that request logs may contain sensitive information

### Scope

The following are in scope for security reports:
- Furl Python package (`pip install furl-ctx`)
- Furl MCP server (`furl_ctx.ccr.mcp_server`)

The following are out of scope:
- Third-party integrations not maintained by us
- Issues in dependencies (report these to the upstream project)
- Social engineering attacks

## Security Features

Furl includes several security features:

- **Credential redaction in logs**: Retrieval-event logs (the `furl_retrieve` event, which previews retrieved content and the query) redact known credential formats on a best-effort basis — JSON and `key=value` secrets (`api_key`, `token`, `secret`, `password`, `credential`, `auth`), `Authorization: Bearer`/`Basic` schemes, and provider prefixes (`sk-`, AWS `AKIA…`, GitHub `gh*_…`). Redaction is pattern-based, so a bare high-entropy secret with no recognizable key name or prefix may not be caught; treat logs as potentially sensitive (see "Log Files" above).
- **Passthrough mode**: Sensitive content passes through unchanged by default
- **Input validation**: All inputs are validated before processing
- **Safe defaults**: Security-conscious defaults out of the box

## Stored originals: at-rest posture

Furl's reversibility works by keeping the **original** content: when the
compression hook or `furl_compress` offloads a payload, the byte-exact original is
written to a local SQLite store — `~/.furl/ccr.sqlite3`, or a per-project
`ccr-ns-<hash>.sqlite3` file in the same directory. Be explicit about what that
means:

- **It is not encrypted.** The store is a plain SQLite database on your local
  disk. The database file is created `0600` (owner read/write only), its parent
  directory `0700`, and the WAL/SHM sidecars inherit the database file's
  permissions — but that is filesystem access control, **not** encryption at rest.
  Anyone who can read your user account's files can read the stored originals.
- **Whatever is in a tool output lands there byte-exact.** If a `Bash` command
  prints an API key, or a log line carries a bearer token, that value is stored
  verbatim and stays retrievable for the retention window (`FURL_CCR_TTL_SECONDS`
  — 30 min for the library, 24h under the Claude Code plugin). The credential
  redaction described above scrubs *retrieval-event log lines* only, **not** the
  stored original.

Mitigations that exist **today** (there is no encryption-at-rest feature — do not
assume one):

- **Opt-in redactor.** Pass a `redactor: str -> str` on `CompressConfig` to scrub
  content **before** it is compressed or stored, so a later `retrieve()` returns
  the already-redacted original. It is fail-closed: a raising redactor aborts
  `compress()` rather than storing unredacted bytes. See
  [LIBRARY.md → "Redact & purge — security"](LIBRARY.md#redact--purge--security).
- **`furl_purge` / `purge(hash)`.** Permanently delete a stored original (one hash,
  or all) — the companion for content captured before a redaction policy existed.
- **TTL expiry.** Entries expire after `FURL_CCR_TTL_SECONDS` and become an
  unrecoverable (loud) miss; shorten it to hold sensitive originals for less time.
- **Per-project isolation.** By default the store is scoped per project, so one
  project's entries never surface in another's `furl_search` / `furl_list` /
  `furl_retrieve`. Setting `FURL_CCR_PROJECT_DIR=""` disables that scoping and
  shares one global store across all projects — convenient, but it widens who can
  retrieve a given original, so leave it scoped when handling sensitive content.

Thank you for helping keep Furl and its users safe!
