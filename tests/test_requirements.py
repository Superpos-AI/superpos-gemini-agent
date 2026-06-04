"""Guard the superpos-agent-core floor.

entrypoint.sh calls `python3 -m superpos_agent_core.github_auth setup`, a module
that only exists in core >= 0.1.2. The pin in requirements.txt must keep that
floor (and stay below the next major) so a cached pre-0.1.2 core can't survive a
Docker rebuild with the new entrypoint.
"""

import pathlib
import re

REQUIREMENTS = pathlib.Path(__file__).resolve().parent.parent / "requirements.txt"


def _core_specifier() -> str:
    for raw in REQUIREMENTS.read_text().splitlines():
        line = raw.strip()
        if line.startswith("#") or not line:
            continue
        if line.lower().startswith("superpos-agent-core"):
            return line
    raise AssertionError("superpos-agent-core not found in requirements.txt")


def test_core_pin_requires_github_auth_release():
    spec = _core_specifier()
    match = re.search(r"~=\s*(\d+)\.(\d+)\.(\d+)", spec)
    assert match, f"expected a compatible-release (~=X.Y.Z) pin, got: {spec!r}"

    major, minor, patch = (int(part) for part in match.groups())
    assert (major, minor, patch) >= (0, 1, 2), (
        "core floor must be >= 0.1.2 — entrypoint.sh uses the github_auth module "
        f"added in that release; got {major}.{minor}.{patch}"
    )
