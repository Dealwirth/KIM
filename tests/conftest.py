"""Shared test configuration.

The Home Assistant test harness ships its own ``custom_components`` package on
``sys.path``. We want our integration to win, so we ensure the repository root
(the directory that contains ``custom_components/kim``) is the *first* entry on
``sys.path`` before anything imports ``custom_components``.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if sys.path[0:1] != [ROOT]:
    while ROOT in sys.path:
        sys.path.remove(ROOT)
    sys.path.insert(0, ROOT)

# drop a possibly already-imported harness package so it is re-resolved
for name in [m for m in list(sys.modules) if m == "custom_components" or m.startswith("custom_components.")]:
    sys.modules.pop(name, None)

# Pre-import *our* package and keep it cached. The harness's ``hass`` fixture
# mounts its own config dir on sys.path and imports ``custom_components``; if
# ours is already in sys.modules that import resolves to this repository.
import custom_components  # noqa: E402

assert "KIM" in custom_components.__file__ or "KIM" in custom_components.__path__[0], (
    f"wrong custom_components resolved: {custom_components.__path__}"
)

pytest_plugins = ["pytest_homeassistant_custom_component"]

# --- scratchpad writer-thread hygiene -------------------------------------
# The scratchpad persists on a daemon writer thread. Home Assistant's test
# harness fails any test that leaves an unexpected thread behind, so we track
# every instance and shut them down after each test.
import weakref  # noqa: E402

from custom_components.kim import scratchpad as _scratchpad_mod  # noqa: E402

_live_pads: "weakref.WeakSet" = weakref.WeakSet()
_orig_init = _scratchpad_mod.Scratchpad.__init__


def _tracking_init(self, *args, **kwargs):
    _orig_init(self, *args, **kwargs)
    _live_pads.add(self)


_scratchpad_mod.Scratchpad.__init__ = _tracking_init


@pytest.fixture(autouse=True)
def _close_scratchpads(verify_cleanup):
    # depending on verify_cleanup forces our teardown to run *before* the
    # harness checks for lingering threads
    yield
    for pad in list(_live_pads):
        pad.close()
