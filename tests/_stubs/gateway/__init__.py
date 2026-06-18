"""Hermes stubs for hermes-agent-independent test runs.

The adapter under test (``platforms/imsg.py``) imports symbols from
``gateway.config`` and ``gateway.platforms.base``.  The test suite does
NOT need the real Hermes install to exercise the adapter's logic — these
stubs mirror only the surface area the adapter and tests actually use.

Why this exists: the live ``gateway.config`` and ``gateway.platforms.base``
are heavy modules that pull in the entire Hermes runtime, network
dependencies, and machine-specific state.  Pulling them into CI breaks
the test pipeline in two ways: (1) they need a live ``HERMES_HOME`` and
real ``imsg`` binary on the path; (2) the live ``PlatformConfig`` and
``BasePlatformAdapter`` have grown optional fields and methods the
adapter does not use, so the import surface drifts between the public
repo (test target) and the live install (development target).

The stubs are intentionally narrow: a ``Platform`` enum with the IMSG
member, a ``PlatformConfig`` dataclass, and the four classes the
adapter imports from ``gateway.platforms.base``.  Nothing more.
"""
