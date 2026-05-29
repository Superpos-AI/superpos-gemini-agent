# Slim-Agent-Gemini

Superpos slim agent backed by Google's **[Antigravity CLI (`agy`)](https://antigravity.google/cli)** — Google's current first-party CLI for the Gemini-3 family.

Sister project to `Slim-Agent-Claude` and `Slim-Agent-Codex` — same architecture, same Superpos integration, same Telegram bot interface; just a different LLM executor.  All the shared runtime lives in [`superpos-agent-core`](https://github.com/Superpos-AI/superpos-agent-core); this repo is a thin shell that contributes:

- `GeminiExecutor` — wraps the `agy` CLI as a subprocess and forwards its plain-text output.
- `GeminiConfig` — adds `gemini_*` env-var bindings on top of `BaseConfig`.
- `GeminiRuntimeConfig` — registers known Gemini models for the `/model list` Telegram command (accepted for compatibility — `agy` itself picks the model under the hood).
- Dockerfile / entrypoint — installs `agy` via the vendored `install-agy.sh` and persists the Google OAuth token under `~/.gemini/antigravity-cli/`.

## Quick start

```bash
cp .env.example .env
# fill in SUPERPOS_*, TELEGRAM_*
docker compose up --build
```

### Authenticating agy

`agy` uses Google OAuth — no API-key env var. After the container is up, run the one-time browser-based login:

```bash
docker compose run --rm -it --entrypoint agy gemini --prompt-interactive 'hi'
```

Follow the printed URL, sign in with a Google account that has Antigravity access. The OAuth token is written to `~/.gemini/antigravity-cli/antigravity-oauth-token` inside the container, which the compose volume bind-mounts to the host so it persists across restarts. Once you've completed this once, restart the agent normally — preflight will detect the token and the agent will start.

## Local dev

```bash
pip install -e .
python -m slim_agent_gemini
```

If you're hacking on `superpos-agent-core` in a sibling directory and want
your changes picked up without re-pushing, uncomment the
`[tool.uv.sources]` block in `pyproject.toml` (or `pip install -e
../superpos-agent-core` first).

## Why agy and not `@google/gemini-cli`?

This repo previously installed `@google/gemini-cli` from npm and parsed its JSONL event stream. Google has since shipped `agy` (Antigravity CLI) as the first-party CLI for Gemini-3 — a static native Go binary, no node runtime needed at run-time, dramatically faster cold-start, and the supported path for current Gemini models. The `gemini-cli` npm wrapper is older and intermittently raced with npm/npx package resolution during boot, sometimes producing `MCP startup failed: ... initialize response` errors on agentic models. Switching to `agy` removes that whole layer.

Trade-offs of the switch:

- **No API-key env var.** `agy` is OAuth-only — see "Authenticating agy" above for the one-time setup.
- **No `--model` flag.** Model selection is internal to `agy`. The `GeminiRuntimeConfig.KNOWN_MODELS` list stays for `/model list` parity but `agy` ignores it.
- **No tool-use notifications on stdout.** `agy --print` emits the final response as plain text; tool activity is logged to `~/.gemini/antigravity-cli/cli.log` instead. The Telegram streamer no longer emits "🔧 calling tool …" lines per tool call (it still streams the response text in chunks).

If you need the older `@google/gemini-cli`-based image for a specific reason, check out the previous tag before this swap.

## Status

The agy CLI interface is moving — see `src/slim_agent_gemini/gemini_executor.py` for notes on which flags have been verified. If you hit unexpected output, run `agy --help` and adjust `_build_gemini_command()` accordingly.
