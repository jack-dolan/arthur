#!/usr/bin/env bash
# Install this repo's git hooks.
#
# Git hooks live in .git/hooks, which is NOT part of the repository and does
# not survive a clone — so the hooks themselves are tracked under
# scripts/hooks/ and this script is the portable part that wires them up.
#
# Run it once per clone (`make setup` does it for you). It is idempotent, and
# safe to re-run after pulling a change to a hook.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
GIT_DIR="$(git rev-parse --git-common-dir)"
case "$GIT_DIR" in
  /*) ;;
  *) GIT_DIR="$ROOT/$GIT_DIR" ;;
esac

SRC_DIR="$ROOT/scripts/hooks"
DEST_DIR="$GIT_DIR/hooks"

if [[ ! -d "$SRC_DIR" ]]; then
  echo "install_hooks: no scripts/hooks directory at $SRC_DIR" >&2
  exit 1
fi

# core.hooksPath overrides .git/hooks entirely. Install where git actually
# looks — installing anywhere else is a silent no-op, and a silently absent
# privacy guard is the failure that matters most.
if CUSTOM_PATH="$(git config --get core.hooksPath 2>/dev/null)"; then
  case "$CUSTOM_PATH" in
    /*) DEST_DIR="$CUSTOM_PATH" ;;
    *) DEST_DIR="$ROOT/$CUSTOM_PATH" ;;
  esac
  echo "install_hooks: core.hooksPath is set; installing into $DEST_DIR"
fi

mkdir -p "$DEST_DIR"

installed=0
for src in "$SRC_DIR"/*; do
  [[ -f "$src" ]] || continue
  name="$(basename "$src")"
  dest="$DEST_DIR/$name"

  # Symlink so that a pulled change to a tracked hook takes effect with no
  # reinstall; copy where symlinks are unavailable.
  rm -f "$dest"
  if ln -s "$src" "$dest" 2>/dev/null; then
    how="linked"
  else
    cp "$src" "$dest"
    how="copied"
  fi
  chmod +x "$dest" 2>/dev/null || true
  echo "install_hooks: $how $name -> $dest"
  installed=$((installed + 1))
done

if [[ "$installed" -eq 0 ]]; then
  echo "install_hooks: scripts/hooks is empty; nothing to install" >&2
  exit 1
fi

echo "install_hooks: $installed hook(s) installed."
