"""Pytest conftest: make ``gateway.*`` importable for the test suite.

The test suite is hermes-agent-independent — it uses minimal stubs
under ``tests/_stubs/`` rather than the live ``~/.hermes/hermes-agent``
checkout.  This was a deliberate change in 2026-06-18: the previous
conftest reached into the user's local Hermes install at
``/Users/soup/.hermes/hermes-agent``, which meant CI (and any other
developer) had to clone the full Hermes repo and maintain a matching
``HERMES_HOME`` to run the test suite.  The stubs reduce the test
runtime from ``~3s`` (live import) to ``<0.2s`` (stub import) and make
the suite pass on a stock macOS-latest GitHub Actions runner.

This conftest:

  1. Inserts the ``tests/_stubs/`` directory at the front of ``sys.path``
     so ``from gateway.config import Platform, PlatformConfig`` resolves
     to the stub before any real Hermes checkout can be picked up.
  2. Loads the repo's ``platforms/imsg.py`` as the ``gateway.platforms.imsg``
     module so the adapter code exercised in tests is the one this repo
     ships (not whatever is pip-installed in another location).
  3. Exposes the symbols tests expect via the module-level ``imsg`` alias.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Path constants
TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
STUBS_DIR = TESTS_DIR / "_stubs"
ADAPTER_PATH = REPO_ROOT / "platforms" / "imsg.py"

# 1. Make stub `gateway.*` importable BEFORE the real Hermes install.
#    Insert at index 0 so any ``PYTHONPATH``/site-packages entries are
#    shadowed when a stub symbol exists.
if str(STUBS_DIR) not in sys.path:
    sys.path.insert(0, str(STUBS_DIR))

# Sanity check: the stubs must resolve and the Platform.IMSG member must
# be present.  If this raises, the stub package is broken — fail loud.
from gateway.config import Platform  # noqa: E402  # sentinel import: fails loud on bad stubs

assert hasattr(Platform, "IMSG"), (
    "Stub gateway.config.Platform is missing IMSG. "
    f"STUBS_DIR={STUBS_DIR}, sys.path[0]={sys.path[0]}"
)

# 2. Load the repo's adapter into the `gateway.platforms.imsg` module
#    so test code does ``from gateway.platforms import imsg`` and gets
#    the repo copy, not the live install.
_spec = importlib.util.spec_from_file_location(
    "gateway.platforms.imsg", str(ADAPTER_PATH)
)
_repo_imsg = importlib.util.module_from_spec(_spec)
sys.modules["gateway.platforms.imsg"] = _repo_imsg
_spec.loader.exec_module(_repo_imsg)

# 3. Expose the symbols tests import directly.
#    Tests do ``from .conftest import imsg`` (or use the package-level
#    import below), so keep this attribute live and pointing at the
#    repo-loaded module object.
imsg = _repo_imsg
