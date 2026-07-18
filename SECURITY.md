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
[github.com/omar-y-abdi/furl-ctx/security/advisories](https://github.com/omar-y-abdi/furl-ctx/security/advisories)
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
- **Known credential shapes are redacted from the stored original by default;
  anything else lands byte-exact.** As of the built-in redaction default (audit
  Crit-4), a set of high-confidence credential patterns — private keys, AWS access
  keys (`AKIA`/`ASIA`), GCP/OpenAI/GitHub/Slack tokens, and JWTs — is scrubbed from
  the stored original **before** it is compressed or written to disk, on every store
  path (the PostToolUse hook and every MCP store). Opt out with
  `FURL_REDACT_BUILTINS=0`. But the built-ins are deliberately narrow, so a secret
  with no recognizable prefix or key name (a bare high-entropy token, a
  custom-format credential) still lands verbatim and stays retrievable for the
  retention window (`FURL_CCR_TTL_SECONDS` — 30 min for the library, 24h under the
  Claude Code plugin). The best-effort *retrieval-event log* redaction described
  above is a separate, narrower surface; to widen what is scrubbed from the stored
  original beyond the built-ins, set `FURL_REDACT_PATTERNS` (below), which redacts
  your own matches **before** anything is compressed or written to disk.

Mitigations that exist **today** (there is no encryption-at-rest feature — do not
assume one):

- **Built-in credential redaction (ON by default; audit Crit-4).** High-confidence
  credential shapes (private keys, AWS `AKIA`/`ASIA` keys, GCP/OpenAI/GitHub/Slack
  tokens, JWTs) are scrubbed from the stored original before it is compressed or
  written, on every store path. No configuration required. Opt out with
  `FURL_REDACT_BUILTINS=0`; extend coverage to your own formats with
  `FURL_REDACT_PATTERNS` (both compose — built-ins run first, then your patterns).
- **Preventive redaction via `FURL_REDACT_PATTERNS` (works from the plugin).** List
  one regex per line — or point at a file with `@/path/to/patterns` — in this
  environment variable, set in the Claude Code plugin's `hooks/hooks.json` /
  `.mcp.json` env or your shell. Every match is replaced with a `[REDACTED:<n>]`
  marker **before** the content is compressed or written to the store, from BOTH
  the PostToolUse hook and every MCP store path — `furl_compress` (including its
  pattern-filtered per-run stores), `furl_read`, and the compression pipeline's
  internal offload. This is the redactor the env-only plugin can actually reach —
  the plugin is configured exclusively through environment variables, so it cannot
  pass a Python callable. A later `retrieve()` returns the already-redacted
  original: the pre-redaction secret is gone from the store **by design**. An
  invalid regex is warned about once (on stderr) and skipped; the remaining
  patterns still apply. Unset (the default) is a no-op — byte-identical behavior.
  Patterns compile with `re.MULTILINE`, so `^`/`$` anchor at line boundaries —
  `^password=\S+` matches a `password=` line anywhere in a tool output, which is
  what anchoring means in line-oriented output. Patterns apply in list order; with
  overlapping patterns a later one can match text an earlier replacement produced,
  nesting markers (e.g. `[REDACTED:[REDACTED:2]]`) — no secret bytes survive
  either way. Example (scrub `key=value` secrets, bearer tokens, and whole
  `password=` lines):

  ```
  FURL_REDACT_PATTERNS='(?i)\b(api[_-]?key|token|secret|password)\s*[=:]\s*\S+
  (?i)\bbearer\s+[A-Za-z0-9._-]{12,}
  ^password=\S+'
  ```

  Two honest limits. (1) Redaction is preventive only for content stored AFTER the
  patterns are set — use `furl_purge` for anything captured before. (2) A
  catastrophically-backtracking pattern (stdlib `re` has no match timeout) can hang
  past the hook's 30 s timeout, in which case the host kills the hook and its
  fail-open contract passes that call's output through **raw and unredacted** —
  redaction degrades to passthrough, it never blocks the tool call. Prefer bounded,
  anchored patterns.

- **Library redactor callback (`CompressConfig.redactor`).** In-process library
  callers can additionally pass a `redactor: str -> str` on `CompressConfig`. It
  **composes** with `FURL_REDACT_PATTERNS` — the env patterns run first, then the
  callback, both applying — and is fail-closed: a raising redactor aborts
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

## Supply chain: how the plugin fetches the engine

The Claude Code plugin does not vendor the Python engine. Its hooks resolve it at
first use with `uv run --no-project --with "furl-ctx[mcp]==1.3.0"`. Be explicit
about that posture (audit High-6):

- **Version-pinned, not hash-pinned.** The engine version is exact
  (`==1.3.0`), so you always get that release, but the download is not verified
  against a recorded artifact hash. `uv`'s inline `--with` has no per-package hash
  channel, so pinning digests would mean shipping and maintaining a full hashed
  requirements lockfile for the engine and every transitive dependency — a change
  large enough to be its own release rather than a bundled fix.
- **`--no-project` bypasses any lockfile**, and the engine's own dependencies
  (`tiktoken`, the `mcp` SDK) resolve to the newest compatible versions at install
  time, so transitive versions can float between installs.
- **Hardening path for high-assurance environments.** The plugin always resolves the
  engine through `uv run --no-project --with 'furl-ctx[mcp]==1.3.0'`. It does not consult
  or prefer a `furl` already on `PATH`, so placing a vetted binary on `PATH` does not
  change what the hooks or the MCP server run. To control the fetch, point `uv` at a
  private index or mirror you trust, or pre-populate `uv`'s cache with a wheel you
  generated and reviewed, so the pinned version resolves from a source you control. Track
  hash-pinning progress in the repository issues.

## Inbound `<<ccr:HASH>>` markers in compressed content

A `<<ccr:HASH>>` marker is furl-ctx's retrieval pointer. When furl-ctx compresses
third-party content, for example a web page or a tool output furl-ctx did not itself
produce, that content could carry attacker-planted text shaped like a real pointer. This
is a same-project confused-deputy risk: on a bulk `resolve_markers` pass, a planted marker
whose hash happens to match a live entry in the same project's store could expand that
entry's original content into the model's context.

What bounds the risk today:

- **Store-lookup gating.** `resolve_markers` and `furl_retrieve` expand a marker only when
  its hash resolves to a real entry in the active store; an unknown hash is returned
  verbatim, never fabricated. A planted marker does nothing unless its hash already keys a
  stored original.
- **Syntactic hash validation.** Only 12-hex or 24-hex lowercase hashes are accepted at
  the retrieval ingress, so malformed marker text is rejected outright.
- **Per-project isolation.** Stores are scoped per project by default, so a planted marker
  cannot reach another project's originals.

### Text-defang mitigation: evaluated and not shipped

A pre-compression text defang for inbound markers was implemented, then
reverted after an adversarial review found it unsound on three counts:

- **Bypassable.** A duplicate decoy bracket marker that repeats the target
  hash defeats a first-occurrence replace, so the real marker survives and
  `resolve_markers` still leaks the unrelated stored content. A hash that is
  not yet stored at defang time defeats a resolve-time check the same way,
  then becomes exploitable once that entry is stored later in the same
  session.
- **Data-lossy.** The same defang mangled Furl's own emitted markers when
  compressed output was re-fed through `compress`, breaking retrieval of that
  offloaded original.
- **Byte-corrupting.** Innocent marker-shaped content built around a hash
  that was never stored still had a sentinel injected, breaking byte-exact
  passthrough.

The root cause is structural: a text-only defang cannot carry provenance to
tell Furl's own markers from a foreign one, so no replace-and-scrub pass over
plain text closes it. The intended fix is a `marker_grammar` provenance
change, where Furl tags or tracks the markers it emits and neutralizes only
markers it did not emit. That fix is scheduled as its own workstream and has
not shipped.

Exploiting the residual above still needs a valid 24 hex digit
content-addressed hash, 96 bits of entropy, that the attacker must already
know or predict, so this is an indirect injection amplifier, not a primary
exfiltration vector. Until the provenance-based fix ships, treat markers
found inside untrusted content as data, not as retrieval instructions, and
rely on the store-lookup gating, hash validation, and per-project isolation
above.

Thank you for helping keep Furl and its users safe!
