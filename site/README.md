# Furl showcase website

A static, dark/amber demonstration deployed at `https://furl-ctx.vercel.app/`.
The six examples replay real Furl **1.2.0** outputs produced on synthetic inputs
by `data/generate.py` on 13 July 2026. They are historical fixtures, not current
production captures or a live upload/compression API. The package's current
version is reported separately. Never relabel archived numbers as a new benchmark.

## Files and preview

`index.html` contains real page content and a no-JavaScript results table.
`assets/app.js` supplies the six accessible demo tabs, folding and retrieval.
`data/furl-data.js` is the prerecorded payload; no visitor content is uploaded.
Fonts and images are self-hosted. `assets/fonts.css` uses the existing WOFF2 files
rather than embedding duplicate base64 font data in the stylesheet.

`privacy.html`, `terms.html`, `cookies.html`, `support.html` and `connect.html`
provide distinct content pages. `404.html` is the custom error document, not a
catch-all rewrite returning 200. Shared legal pages do not load the demo JS.
There are no analytics integrations, tracking cookies or third-party embeds.
Do not add a consent banner for tracking the site does not perform.

Serve the website over HTTP (root-relative links deliberately require a server):

```sh
python3 -m http.server 8000 --directory site
```

Visit `/index.html` or the individual `.html` files with that simple server.
The browser-test server emulates Vercel's clean URLs, security headers and deep
404 behavior more closely. Do not open the page with `file://`.

## Tests

From the repository root:

```sh
python3 -m unittest discover -s tests/site -p 'test_*.py'
python3 -m pip install playwright==1.57.0
python3 -m playwright install chromium
npm pack axe-core@4.10.3 --pack-destination /tmp
mkdir -p /tmp/furl-axe
tar -xzf /tmp/axe-core-4.10.3.tgz -C /tmp/furl-axe
python3 tests/site/browser.py --axe /tmp/furl-axe/package/axe.min.js
```

The browser suite covers desktop/mobile pages, axe WCAG rules, keyboard tabs,
all six fold/retrieve/reset interactions, the reset-animation race, reduced
motion, no-JavaScript content, empty browser storage, request failures and
third-party requests. Reports and screenshots go to `artifacts/site/`.
Use `--origin` for an already running deployment. Automated checks supplement,
not replace, manual accessibility review.

Static tests enforce unique metadata, one H1 per content page, real links,
sitemap coverage, source-map exclusion, icon sizes and CSP hashes for JSON-LD.
Verification responses such as Google's ownership file are not content pages.
Update CSP hashes in `vercel.json` when changing inline structured data; never
work around a mismatch by enabling `unsafe-inline` scripts. No LocalBusiness
schema is used because Furl is software, not a physical local business.

## Deployment and assets

Keep Vercel's Root Directory set to **`site`**, with no framework or build command.
`vercel.json` enables clean URLs and security/cache headers. `.vercelignore`
excludes Python generators, README and source maps from the public deployment.
The project's existing ignored-build command skips commits with no changes in
`site/`; a cancelled no-change build is expected, not an application failure.
A feature PR previews site changes; merging main publishes through the existing
Git integration. Do not change an unrelated Vercel project.

Directory icons are under `assets/icons/directory-512.png` and
`directory-256.png`; ChatGPT composer icons are `composer-128.png` and
`composer-48.png`. All are square PNGs below 10 KiB. The social card is
`assets/og.png`, 1200 x 630 pixels. Reuse the same mark consistently. Fonts are
not part of the downloadable icon bundle.

## MCP is a separate runtime

This static site is not an MCP server and `/mcp` is not a working public service
merely because a URL has been written into documentation. See
[`deploy/remote`](../deploy/remote/README.md) for the implemented Streamable HTTP
resource server with OAuth token verification and isolated durable CCR workers.
It still requires an approved host, real identity provider, persistent volume,
TLS, domain-verification token and end-to-end testing before public launch.
The local stdio server can continue to use a private Secure MCP Tunnel.

The site's legal pages describe actual static-site/local behavior. Publish a
service-specific notice with confirmed providers and retention before accepting
public user data. Do not invent OAuth endpoints, business details, token-savings
guarantees or verification tokens.
