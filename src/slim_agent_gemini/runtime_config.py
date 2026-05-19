"""Gemini runtime config: registers known models for /model list."""

from __future__ import annotations

from superpos_agent_core import RuntimeConfig


class GeminiRuntimeConfig(RuntimeConfig):
    """Runtime knobs specialized for Gemini models."""

    KNOWN_MODELS: tuple[str, ...] = (
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
        "gemini-2.0-flash-thinking-exp",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
    )

    EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high")
