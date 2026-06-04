"""Guard the superpos-agent-core floor.

entrypoint.sh calls `python3 -m superpos_agent_core.github_auth setup`, a module
that only exists in core >= 0.1.2. The pins in both requirements.txt and
pyproject.toml must keep that floor (and stay below the next major) so a cached
pre-0.1.2 core can't survive a Docker rebuild with the new entrypoint.
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"
PYPROJECT = ROOT / "pyproject.toml"

# Matches the core pin in either a bare requirements line
# (superpos-agent-core~=0.1.2) or a quoted pyproject entry
# ("superpos-agent-core~=0.1.2",).
_CORE_PIN = re.compile(r'superpos-agent-core~=\s*(\d+)\.(\d+)\.(\d+)')


def _core_specifier(path: pathlib.Path) -> str:
    for raw in path.read_text().splitlines():
        line = raw.strip().strip('"').strip("'")
        if line.startswith("#") or not line:
            continue
        if line.lower().lstrip('"\'').startswith("superpos-agent-core"):
            return line
    raise AssertionError(f"superpos-agent-core not found in {path.name}")


@pytest.mark.parametrize("path", [REQUIREMENTS, PYPROJECT], ids=lambda p: p.name)
def test_core_pin_requires_github_auth_release(path):
    spec = _core_specifier(path)
    match = _CORE_PIN.search(spec)
    assert match, f"expected a compatible-release (~=X.Y.Z) pin, got: {spec!r}"

    major, minor, patch = (int(part) for part in match.groups())
    assert (major, minor, patch) >= (0, 1, 2), (
        "core floor must be >= 0.1.2 — entrypoint.sh uses the github_auth module "
        f"added in that release; got {major}.{minor}.{patch}"
    )
