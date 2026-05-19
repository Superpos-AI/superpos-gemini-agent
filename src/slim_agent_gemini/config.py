"""Gemini-specific config: extends BaseConfig with Google API key + model fields."""

from __future__ import annotations

import os
from dataclasses import dataclass

from superpos_agent_core import BaseConfig


@dataclass
class GeminiConfig(BaseConfig):
    """Adds Gemini CLI-specific knobs on top of the universal BaseConfig."""

    google_api_key: str = ""  # GEMINI_API_KEY or GOOGLE_API_KEY
    gemini_model: str = "gemini-2.5-pro"
    gemini_reasoning_effort: str = "medium"  # low|medium|high

    def __post_init__(self) -> None:
        if not self.executor_kind or self.executor_kind == "generic":
            self.executor_kind = "gemini"
        super().__post_init__()

    @classmethod
    def from_env(cls) -> "GeminiConfig":
        base = cls._base_env_kwargs()
        base.update(
            executor_kind="gemini",
            google_api_key=(
                os.environ.get("GEMINI_API_KEY", "")
                or os.environ.get("GOOGLE_API_KEY", "")
            ),
            gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.5-pro"),
            gemini_reasoning_effort=os.environ.get("GEMINI_REASONING_EFFORT", "medium"),
        )
        # Override executor_max_turns from a Gemini-specific env var if set
        if os.environ.get("GEMINI_MAX_TURNS"):
            base["executor_max_turns"] = int(os.environ["GEMINI_MAX_TURNS"])
        return cls(**base)
