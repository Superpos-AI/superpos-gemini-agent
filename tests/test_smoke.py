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
