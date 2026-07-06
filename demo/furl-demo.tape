# VHS tape — regenerates the README demo gif with current Furl branding.
#
#   brew install vhs        # or: https://github.com/charmbracelet/vhs
#   vhs demo/furl-demo.tape # writes FurlDemo-Fast.gif at repo root
#
# The recorded command is demo/furl_demo.py — a real, deterministic Furl
# compression (a noisy CI log → a fraction of the tokens, the FATAL needle
# preserved). Keep the README caption in sync with its printed numbers.

Output FurlDemo-Fast.gif

Require python

Set Shell "bash"
Set FontSize 20
Set Width 960
Set Height 520
Set Padding 24
Set Theme "Dracula"
Set PlaybackSpeed 1.0

Hide
Type "cd $(git rev-parse --show-toplevel)"
Enter
# Use the project venv if present, else the ambient python.
Type "PY=.venv/bin/python; [ -x $PY ] || PY=python"
Enter
Show

Sleep 500ms
Type "$PY demo/furl_demo.py"
Sleep 800ms
Enter
Sleep 4s
