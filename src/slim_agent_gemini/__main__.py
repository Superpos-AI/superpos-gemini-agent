"""Slim-Agent-Gemini entry point — wires GeminiExecutor into the core orchestrator."""

from __future__ import annotations

import asyncio
import logging

from slim_agent_core import run_agent

from .config import GeminiConfig
from .gemini_executor import GeminiExecutor
from .runtime_config import GeminiRuntimeConfig

log = logging.getLogger(__name__)


async def main() -> None:
    config = GeminiConfig.from_env()
    runtime = GeminiRuntimeConfig.load(
        default_model=config.gemini_model,
        default_effort=config.gemini_reasoning_effort,
        home_dir=config.home_dir,
    )

    def _factory(cfg, rt, superpos, gateway, persona):
        return GeminiExecutor(cfg, rt, superpos, gateway, persona=persona)

    await run_agent(config, runtime, executor_factory=_factory)


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    cli()
