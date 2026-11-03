"""Shared fixtures.

The telemetry modules live in ../telemetry and are imported as top-level
modules (server.py does the same: it runs as a script, not as a package).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TELEMETRY_DIR = Path(__file__).resolve().parent.parent / "telemetry"
if str(TELEMETRY_DIR) not in sys.path:
    sys.path.insert(0, str(TELEMETRY_DIR))


@pytest.fixture
def derived_state():
    """A fresh DerivedState with a known cell count."""
    import derived
    return derived.DerivedState(batt_cells=3)
