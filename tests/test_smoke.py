"""Smoke tests — verify the package is importable and exports are intact."""


def test_package_importable():
    """The top-level package should be importable without side-effects."""
    import slim_agent_gemini  # noqa: F811

    assert slim_agent_gemini.__all__ is not None


def test_public_exports():
    """All declared public names should be importable."""
    from slim_agent_gemini import GeminiConfig, GeminiExecutor, GeminiRuntimeConfig

    assert GeminiConfig is not None
    assert GeminiExecutor is not None
    assert GeminiRuntimeConfig is not None


def test_model_info_returns_static_agy_values():
    """model_info() reports the fixed agy identifier, not user-configurable runtime values."""
    import os
    import tempfile
    from unittest.mock import patch

    from slim_agent_gemini import GeminiConfig, GeminiExecutor, GeminiRuntimeConfig

    with tempfile.TemporaryDirectory() as tmpdir:
        config = GeminiConfig(
            home_dir=tmpdir,
            gemini_model="gemini-2.5-pro",
            gemini_reasoning_effort="medium",
            executor_max_parallel=1,
        )
        runtime = GeminiRuntimeConfig(
            model="gemini-2.5-pro",
            effort="medium",
            path=os.path.join(tmpdir, "rt.json"),
        )
        with patch.object(GeminiExecutor, "_inject_persona_into_gemini_md"):
            executor = GeminiExecutor(config, runtime, superpos=None, gateway=None)

        info = executor.model_info()
        assert info == {"model": "agy", "effort": "default"}, (
            f"model_info should return static agy values, got {info}"
        )

        # Changing runtime values should NOT affect model_info
        runtime.model = "gemini-1.5-flash"
        runtime.effort = "high"
        assert executor.model_info() == {"model": "agy", "effort": "default"}
