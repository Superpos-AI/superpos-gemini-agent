# Slim-Agent-Gemini

Superpos slim agent backed by [Google's Gemini CLI](https://github.com/google-gemini/gemini-cli).

Sister project to `Slim-Agent-Claude` and `Slim-Agent-Codex` — same architecture, same Superpos integration, same Telegram bot interface; just a different LLM executor.  All the shared runtime lives in [`superpos-agent-core`](https://github.com/Superpos-AI/superpos-agent-core); this repo is a thin shell that contributes:

- `GeminiExecutor` — wraps the `gemini` CLI as a subprocess and parses its JSONL events.
- `GeminiConfig` — adds `gemini_*` and `google_api_key` env-var bindings on top of `BaseConfig`.
- `GeminiRuntimeConfig` — registers known Gemini models for the `/model list` Telegram command.
- Dockerfile / entrypoint — installs `@google/gemini-cli` from npm and wires Google API credentials.

## Quick start

```bash
cp .env.example .env
# fill in SUPERPOS_*, TELEGRAM_*, GEMINI_API_KEY
docker compose up --build
```

## Local dev

```bash
pip install -e .
python -m slim_agent_gemini
```

If you're hacking on `slim-agent-core` in a sibling directory and want
your changes picked up without re-pushing, uncomment the
`[tool.uv.sources]` block in `pyproject.toml` (or `pip install -e
../slim-agent-core` first).

## Status

The Gemini CLI interface is still iterating — see `src/slim_agent_gemini/gemini_executor.py` for notes on which flags / event types have been verified vs. assumed-from-Codex-parity.  If you hit unexpected output, run `gemini --help` and adjust `_build_gemini_command()` accordingly.
