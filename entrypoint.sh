#!/bin/bash
set -e

# Configure git identity if provided
if [ -n "$GIT_USER_NAME" ]; then
    git config --global user.name "$GIT_USER_NAME"
fi
if [ -n "$GIT_USER_EMAIL" ]; then
    git config --global user.email "$GIT_USER_EMAIL"
fi

# Configure GitHub CLI auth if token provided.
#
# We intentionally do NOT use `git config --global url.<token-URL>.insteadOf`
# here — that pattern embeds the token into every clone's .git/config as the
# origin remote, so `git remote -v` prints the token in cleartext.  Instead we
# let `gh` register a credential helper; git fetches the token on demand and
# never persists it into repo configs or remote URLs.
if [ -n "$GITHUB_TOKEN" ]; then
    echo "$GITHUB_TOKEN" | gh auth login --with-token 2>/dev/null || true
    gh auth setup-git 2>/dev/null || true
fi

# Materialize GEMINI_API_KEY into ~/.gemini/auth.json if the CLI needs it on
# disk (some Gemini CLI versions don't read env vars on subsequent calls).
mkdir -p "$HOME/.gemini"
if [ -n "$GEMINI_API_KEY" ] && [ ! -f "$HOME/.gemini/auth.json" ]; then
    cat > "$HOME/.gemini/auth.json" <<EOF
{"api_key": "$GEMINI_API_KEY", "auth_mode": "apikey"}
EOF
    chmod 600 "$HOME/.gemini/auth.json"
    echo "[entrypoint] Wrote $HOME/.gemini/auth.json from GEMINI_API_KEY env" >&2
fi

# Run module setup: install deps, symlink scripts onto PATH, update GEMINI.md.
# --bin-dir links scripts from both workspace and core-bundled modules,
# so platform tools (e.g. superpos-issues) work even when nothing is in
# /workspace/.gemini/modules.
#
# If this fails (network blip on `pip install`, broken module, etc.) the
# container still starts — the Dockerfile pre-populated modules-bin with
# build-time symlinks to workspace scripts, so those stay callable from
# PATH.  Only core-bundled tools (added at runtime) are lost in that
# degraded mode.
python3 -m superpos_agent_core.module_setup \
    --modules-dir /workspace/.gemini/modules \
    --agents-md /workspace/GEMINI.md \
    --bin-dir /workspace/.gemini/modules-bin \
    || echo "Warning: module setup failed (build-time workspace symlinks remain in place)"

exec "$@"
