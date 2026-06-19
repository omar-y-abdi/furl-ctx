# archive/

Code/docs cut from the active tree by the **cut-and-archive** pass (driven by `lazy-dev-AUDIT.md`, 2026-06-19).
Nothing here is deleted — it is **moved, not removed**, so every cut is reversible.

## Why moved, not deleted
The user directive was "cut all code, archive anything important, then test it still runs." Each item below was
moved here and the tree re-tested with two gates:
- **G1** full `pytest` suite
- **G2** public-surface walk: every `headroom.__all__` / `_LAZY_EXPORTS` entry must resolve

A move is kept only if **both gates stayed green**. Anything that turned a gate red was restored to the live tree
(see `RESTORE-LOG.md`) — that is empirical proof it was load-bearing, which beats the static audit label.

## How to restore an item
```bash
git mv archive/<path> <original-path>      # history is preserved (these are renames)
# then re-add any export entry that was removed from headroom/__init__.py / cache/__init__.py / ccr/__init__.py
```

## Contents (batch B1 — pure-dead, zero code coupling)
- `REALIGNMENT/` — Phase A–I planning/design docs (project roadmap; archived, not deleted, per "may seem important").
- `sql/` — Supabase telemetry schema (proxy feature retired; `TelemetryBeacon` never instantiated).
- `claude_analysis_ttl.py` — standalone cache-TTL cost script, zero headroom imports.
- `Dockerfile`, `docker-compose.yml`, `docker-bake.hcl`, `docker/`, `.dockerignore` — image can't build (undefined `[proxy,code]` extra, missing `headroom` console script). NOTE: a `.github/workflows/docker.yml` CI job may still reference these.
- `PR.md`, `TESTING-copilot-subscription.md`, `ENTERPRISE.md`, `.changelog.md` — stale internal/marketing docs.

Empty stray dirs `headroom/{integrations,observability,storage}/` were removed (no tracked content to archive).
