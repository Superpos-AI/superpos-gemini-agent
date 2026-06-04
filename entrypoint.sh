#!/bin/bash
set -e

# Configure git identity if provided
if [ -n "$GIT_USER_NAME" ]; then
    git config --global user.name "$GIT_USER_NAME"
fi
if [ -n "$GIT_USER_EMAIL" ]; then
    git config --global user.email "$GIT_USER_EMAIL"
fi

# Configure GitHub auth. Centralized in agent-core so every agent stays in sync.
# Token precedence (static GITHUB_TOKEN is canonical and never expires):
#   1. GITHUB_TOKEN set → gh auth login --with-token + gh auth setup-git.
#   2. else a github_app service connection is discoverable → register a git
#      credential helper that mints short-lived installation tokens from the
#      Superpos broker on demand (no long-lived secret in the container).
#   3. else → direct git/gh auth skipped; GitHub is still reachable through the
#      Superpos proxy (the `superpos-github` module).
#
# We deliberately avoid `git config url.<token>.insteadOf`, which would bake the
# token into every clone's .git/config and leak it via `git remote -v`; both the
# gh credential helper and the broker helper hand tokens to git on demand only.
python3 -m superpos_agent_core.github_auth setup \
    || echo "Warning: GitHub auth setup failed (proxy access via superpos-github still works)"

# Make sure the agy config directory exists before any module setup
# runs (module_setup writes GEMINI.md and may also touch settings under
# ~/.gemini). agy uses OAuth — there is no API-key env-var to materialise.
# First-time OAuth setup is documented in README ("Authenticating agy").
# The OAuth token lives under ~/.gemini/antigravity-cli/ which the
# compose file bind-mounts so it persists across container restarts.
mkdir -p "$HOME/.gemini/antigravity-cli"
if [ -n "${GEMINI_API_KEY:-}" ]; then
    echo "[entrypoint] WARNING: GEMINI_API_KEY is set but agy ignores it." >&2
    echo "[entrypoint]          agy uses Google OAuth — see README for one-time setup." >&2
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
