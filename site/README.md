# furl showcase site

A self-contained static site that lets a visitor watch furl fold real tool
output and pull originals back byte-exact. Every number, marker, and retrieved
byte is real furl output captured by `data/generate.py`. Nothing is hand-typed.

## What is here

```
site/
  index.html            page shell (nav, hero, demo mount, how it works, honest read)
  assets/
    styles.css          full design system + fold and retrieve mechanics
    app.js              vanilla classic script: tabs, fold animation, retrieve reveal
    fonts/              self-hosted woff2 (JetBrains Mono, Bricolage Grotesque) + FONTS.md
  data/
    generate.py         the generator: calls furl, writes the JSON below
    <id>.json           one honest record per capture (logs, crash, json, pytest, ci, csv)
    manifest.json       metadata + aggregate
    furl-data.js        window.__FURL_DATA__, consumed by the page (no runtime fetch)
  vercel.json           static headers
```

The page reads `data/furl-data.js` through a plain `<script>` tag, so it works
opened straight from disk as `file://` as well as served over http. There are
no external CDN, font, or script requests at runtime.

## Preview locally

Open the file directly:

```
open site/index.html
```

Or serve it, which is closer to production:

```
cd site
python3 -m http.server 8000
# then open http://localhost:8000
```

## Regenerate the data

The numbers come from the real `furl_ctx` engine. From the repository root,
in a Python environment where furl is importable:

```
python3 -m venv .venv && . .venv/bin/activate
pip install -e .            # or: maturin develop --release
python site/data/generate.py
```

This rewrites `data/*.json` and `data/furl-data.js`. The script prints a
self-check table with the real per-capture savings and whether each full
retrieve came back byte-exact. Sizes are chosen so each capture crosses furl's
offload threshold and renders in full, the way a real agent's tool output does.

## Deploy to Vercel

The site is static, no build step. Point Vercel at this `site/` directory as
the project root and deploy. From the CLI:

```
cd site
vercel deploy --prod
```

Or set the project Root Directory to `site` in the Vercel dashboard. There is
no framework and no build command.

## Honest notes

- These captures are repetitive machine output, the case furl is built for. On
  high-entropy prose the honest range is lower, roughly zero to 54 percent.
- Automatic hands-off compression works on Claude Code 2.1.163 and newer; the PostToolUse
  hook mirrors each replacement to the tool's output shape, so the harness honors it. It was
  built for issue 68951. The furl API and the MCP tools also deliver verified compression on
  every version, and every original returns byte-exact through retrieve. That is what this
  page shows.
