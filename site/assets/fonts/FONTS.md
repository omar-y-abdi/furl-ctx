# Fonts

Self-hosted, latin subset, woff2. No runtime CDN request. Both families are
licensed under the SIL Open Font License 1.1, which permits self-hosting and
redistribution.

These woff2 are base64-embedded into `../fonts.css` by `../build-fonts.py`, so
the page loads `fonts.css` rather than these files directly. Data URIs load
under `file://` as well as http, and arrive at CSS parse time, so there is no
separate request and no font-swap layout shift.

| File | Family | Weight | Role |
|------|--------|--------|------|
| `jetbrains-mono-400.woff2` | JetBrains Mono | 400 | payloads, code, data, UI |
| `jetbrains-mono-500.woff2` | JetBrains Mono | 500 | uppercase labels, eyebrows |
| `jetbrains-mono-700.woff2` | JetBrains Mono | 700 | strong data values, buttons |
| `bricolage-grotesque-700.woff2` | Bricolage Grotesque | 700 | secondary headings, tab labels |
| `bricolage-grotesque-800.woff2` | Bricolage Grotesque | 800 | hero, section titles, wordmark |

## Source and refetch

Files were pulled from Fontsource on jsDelivr, which serves pre-subset OFL
woff2 at stable URLs. To refetch:

```
base="https://cdn.jsdelivr.net/fontsource/fonts"
curl -sL "$base/jetbrains-mono@latest/latin-400-normal.woff2"      -o jetbrains-mono-400.woff2
curl -sL "$base/jetbrains-mono@latest/latin-500-normal.woff2"      -o jetbrains-mono-500.woff2
curl -sL "$base/jetbrains-mono@latest/latin-700-normal.woff2"      -o jetbrains-mono-700.woff2
curl -sL "$base/bricolage-grotesque@latest/latin-700-normal.woff2" -o bricolage-grotesque-700.woff2
curl -sL "$base/bricolage-grotesque@latest/latin-800-normal.woff2" -o bricolage-grotesque-800.woff2
```

Licenses: JetBrains Mono OFL, https://github.com/JetBrains/JetBrainsMono
Bricolage Grotesque OFL, https://github.com/ateliertriay/bricolage
