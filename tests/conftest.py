"""Pytest bootstrap for the Hermes Assistant plugin test-suite.

The tests exercise real plugin code that imports Hermes Agent modules
(`gateway.*`, `hermes_constants`), so they require a checkout of the
Hermes Agent source. Point them at it with the HERMES_AGENT_SRC
environment variable, e.g.:

    HERMES_AGENT_SRC=/path/to/hermes-agent python -m pytest
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_agent_src = os.environ.get("HERMES_AGENT_SRC")
if _agent_src and Path(_agent_src).is_dir() and _agent_src not in sys.path:
    sys.path.insert(0, _agent_src)

try:
    import hermes_constants  # noqa: F401
except ImportError:  # pragma: no cover - environment guard
    import pytest

    pytest.exit(
        "Hermes Agent source not importable: these tests require a checkout of "
        "the Hermes Agent repository. Set HERMES_AGENT_SRC=/path/to/hermes-agent "
        "and run pytest again.",
        returncode=1,
    )
