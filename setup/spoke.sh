#!/usr/bin/env bash
# setup/spoke.sh -- what every herdr-machine0 spoke needs, nothing personal.
#
# The hub pushes the plugin to ~/.local/share/herdr-machine0/plugin and runs
# this there (image builds, GPU spokes). Then the hub's optional
# `provision_command` adds the user's own setup. Idempotent; Ubuntu/Debian.
set -euo pipefail

PLUGIN=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
BIN=$HOME/.local/bin
mkdir -p "$BIN" ~/.config/herdr-machine0
export PATH="$BIN:$PATH"

log() { printf '==> %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

log "packages"
# A fresh cloud VM is still running cloud-init and unattended-upgrades.
have cloud-init && { sudo cloud-init status --wait >/dev/null 2>&1 || true; }
APT=(sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=900 -qq)
missing=()
for pkg in dtach python3 git curl jq rsync; do have "$pkg" || missing+=("$pkg"); done
if (( ${#missing[@]} )); then
    "${APT[@]}" update
    "${APT[@]}" install -y --no-install-recommends "${missing[@]}"
fi

log "herdr (CLI only: spokes report to the hub's server)"
have herdr || curl -fsSL https://herdr.dev/install.sh | sh

log "agent harnesses"
npm_global() {
    # User-level prefix: no root needed whatever installed node.
    npm install -g --prefix "$HOME/.local" --no-audit --no-fund --loglevel=error "$@"
}
if have npm; then
    have pi || npm_global @earendil-works/pi-coding-agent
    have codex || npm_global @openai/codex
    have opencode || npm_global --allow-scripts=opencode-ai opencode-ai || npm_global opencode-ai
else
    echo "npm not found: install Node.js 20+ to get pi, codex and opencode" >&2
fi
have claude || curl -fsSL https://claude.ai/install.sh | bash

log "herdr integrations"
mkdir -p ~/.pi/agent ~/.claude ~/.codex ~/.config/opencode
for agent in pi claude codex opencode; do
    herdr integration install "$agent" >/dev/null 2>&1 || echo "  herdr integration $agent: skipped"
done

log "spoke CLI and pi extension"
ln -sfn "$PLUGIN/spoke.py" "$BIN/spoke"
printf 'spoke\n' > ~/.config/herdr-machine0/role
"$BIN/spoke" install-pi-extension

log "sshd: a reconnect may replace a slot's stale relay socket"
if [[ -d /etc/ssh/sshd_config.d ]]; then
    printf '# written by herdr-machine0\nStreamLocalBindUnlink yes\n' |
        sudo tee /etc/ssh/sshd_config.d/herdr-machine0.conf >/dev/null
    sudo systemctl reload ssh 2>/dev/null || sudo systemctl reload sshd 2>/dev/null || true
fi

log "spoke ready"
