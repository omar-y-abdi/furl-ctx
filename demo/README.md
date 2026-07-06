# Demo

`furl_demo.py` is the README hero gif as runnable, deterministic code: a noisy
CI log is compressed with Furl, printing the token drop and proving the buried
`FATAL` line survives (visible in the output or recoverable via `furl_retrieve`).

```bash
python demo/furl_demo.py
```

Current output (reproducible — quote these in the README caption):

```
CI log in     :  3,781 tokens
Furl out      :    164 tokens   (96% fewer)
FATAL needle  : still visible
```

## Regenerating the gif

The root `FurlDemo-Fast.gif` is rendered from `furl-demo.tape` with
[`vhs`](https://github.com/charmbracelet/vhs):

```bash
brew install vhs          # or see the vhs repo for other platforms
vhs demo/furl-demo.tape   # writes FurlDemo-Fast.gif at the repo root
```

Re-run this after any branding or demo change so the gif never drifts from the
product (the committed gif predates the Furl rename and should be regenerated).
