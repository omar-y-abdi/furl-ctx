#!/usr/bin/env bash
set -euo pipefail
ARTIFACT_DIR="${1:-/mnt/data}"
DEST="${2:-/mnt/data/furl-ctx}"
ZIP="$ARTIFACT_DIR/chat-dev-repo-bundle.zip"
[[ -f "$ZIP" ]] || { echo "error: missing $ZIP" >&2; exit 2; }
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
unzip -q -o "$ZIP" -d "$WORK"
BUNDLE=$(find "$WORK" -type f -name '*.bundle' -print -quit)
[[ -n "$BUNDLE" ]]
rm -rf "$DEST"
git clone "$BUNDLE" "$DEST"
git -C "$DEST" switch --detach >/dev/null
MAIN_SHA=$(git bundle list-heads "$BUNDLE" | awk '$2=="refs/remotes/origin/main" {print $1; exit}')
CHAT_DEV_SHA=$(git bundle list-heads "$BUNDLE" | awk '$2=="refs/remotes/origin/chat-dev" {print $1; exit}')
[[ -n "$MAIN_SHA" ]] && git -C "$DEST" branch -f main "$MAIN_SHA"
if [[ -n "$CHAT_DEV_SHA" ]]; then
  git -C "$DEST" branch -f chat-dev "$CHAT_DEV_SHA"
  git -C "$DEST" switch chat-dev
else
  git -C "$DEST" switch main
fi
git -C "$DEST" remote set-url origin https://github.com/omar-y-abdi/furl-ctx.git
git -C "$DEST" fsck --full --no-dangling
echo "bootstrap checkout ready: $DEST @ $(git -C "$DEST" rev-parse HEAD)"
echo "local main snapshot: $(git -C "$DEST" rev-parse main)"
echo "after devkit install/verification, switch to main before product work"
