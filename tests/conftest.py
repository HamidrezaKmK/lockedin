"""pytest convenience layer on top of the real mechanism: environment variables.

The repo is normally run with ``python -m unittest discover``, where ``tests/__init__.py``
alone sets the safe defaults. This file only adds a friendlier on-ramp for pytest users; it must
stay harmless when pytest itself is not installed or not the runner in use (nothing outside this
file imports it, and pytest only loads it when it is present in a collected rootdir).
"""
from __future__ import annotations

import os


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--live-model", action="store_true", default=False,
        help="Permit tests that call a paid model or launch a real coding agent "
             "(sets LOCKEDIN_LIVE_MODEL=1 and lifts LOCKEDIN_AGENT_TURNS=off). "
             "Without this flag those tests are skipped, and no test spends model tokens.",
    )


def pytest_configure(config) -> None:
    if config.getoption("--live-model"):
        os.environ["LOCKEDIN_LIVE_MODEL"] = "1"
        os.environ.pop("LOCKEDIN_AGENT_TURNS", None)
    else:
        os.environ.setdefault("LOCKEDIN_AGENT_TURNS", "off")
        os.environ.pop("LOCKEDIN_LIVE_MODEL", None)
