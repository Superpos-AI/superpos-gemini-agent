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

### Authenticating agy (one-time)

`agy` uses Google OAuth — no API-key env var. Run a one-shot login before bringing the agent up (matches the `superpos-claude-agent` pattern of `docker run -it --entrypoint claude ...`):

```bash
docker run -it --rm --network=host \
  -v agy_auth:/home/agent/.gemini \
  --entrypoint agy slim-gemini-agent:local
```

Two important flags:

* `--network=host` — agy's OAuth token-exchange step tries IPv6 first, and Docker bridges typically don't have IPv6 routing. Host networking sidesteps that (only needed for the login step; the running agent works fine on bridge).
* `-v agy_auth:/home/agent/.gemini` — persists the OAuth token in a named volume. The agent's compose file should mount the same volume.

agy will print a URL like `https://accounts.google.com/o/oauth2/auth?...`. Open it in your browser, sign in with a Google account that has Antigravity access, copy the `code=` parameter from the callback URL, paste it back into the terminal. Once it confirms "Signing in…", you can `Ctrl+C` to exit. The OAuth token persists under `~/.gemini/antigravity-cli/antigravity-oauth-token` in the volume and will be reused by `agy --print` invocations until the refresh token expires (typically weeks).

### Running

```bash
docker compose up --build
```

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

- **No API-key env var.** `agy` is OAuth-only — see "Authenticating agy (one-time)" above. Operationally this means one interactive step before first run; subsequent runs are headless.
- **No `--model` flag.** Model selection is internal to `agy`. The `GeminiRuntimeConfig.KNOWN_MODELS` list stays for `/model list` parity but `agy` ignores it.
- **No tool-use notifications on stdout.** `agy --print` emits the final response as plain text; tool activity is logged to `~/.gemini/antigravity-cli/cli.log` instead. The Telegram streamer no longer emits "🔧 calling tool …" lines per tool call (it still streams the response text in chunks).
- **No `--dangerously-skip-permissions`.** Listed in `agy --help` but as of agy 1.0.3 its argparse is broken — the flag consumes the next positional as its value, eating the prompt. Unnecessary anyway: in `--print` mode there is no interactive UI to challenge tool calls, so tools execute without blocking.

If you need the older `@google/gemini-cli`-based image for a specific reason, check out the previous tag before this swap.

## Status

The agy CLI interface is moving — see `src/slim_agent_gemini/gemini_executor.py` for notes on which flags have been verified. If you hit unexpected output, run `agy --help` and adjust `_build_gemini_command()` accordingly.
