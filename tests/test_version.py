"""The package version must match what will actually be published."""
import re
from pathlib import Path

import bb_sapi

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _declared_version() -> str:
    # requires-python is >=3.10, so tomllib is not available on every supported
    # interpreter; a regex over the one line we need avoids the dependency.
    match = re.search(r'^version = "([^"]+)"', PYPROJECT.read_text(), re.MULTILINE)
    assert match, "no version declared in pyproject.toml"
    return match.group(1)


def test_version_matches_pyproject():
    """Guards the drift that left the repo on 0.1.0 while PyPI served 1.0.0.

    __version__ reads the installed distribution's metadata, so a mismatch here
    means the working tree declares a version that was never installed — either
    reinstall (``pip install -e .``) or the two genuinely disagree.
    """
    assert bb_sapi.__version__ == _declared_version()


def test_version_is_a_release_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+", bb_sapi.__version__), (
        f"{bb_sapi.__version__!r} is not a released version — the package is "
        f"not installed, so its metadata could not be read."
    )
