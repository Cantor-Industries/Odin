# Copyright 2026 Cantor Industries
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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
