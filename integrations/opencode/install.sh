#!/usr/bin/env bash
# Install (or refresh) the ai-memory OpenCode plugin.
#
#   ./install.sh                 # copy into ~/.config/opencode/plugins/ai-memory
#   PLUGIN_DIR=/path ./install.sh
#
# The repo copy is the source of truth; this copies index.ts over the deployed
# plugin and leaves node_modules alone (OpenCode resolves @opencode/plugin from
# the plugin directory).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="${PLUGIN_DIR:-${HOME}/.config/opencode/plugins/ai-memory}"

mkdir -p "$dest"
install -m 644 "$here/index.ts" "$dest/index.ts"
if [ ! -f "$dest/package.json" ]; then
  install -m 644 "$here/package.json" "$dest/package.json"
fi
if [ ! -d "$dest/node_modules" ]; then
  echo "installing plugin dependencies in $dest"
  (cd "$dest" && npm install --omit=dev --no-audit --no-fund)
fi

echo "installed -> $dest/index.ts"
echo "OpenCode reloads plugins on file change; check ${AI_MEMORY_HOOK_TRACE:-/tmp/ai-memory-hook.log}"
