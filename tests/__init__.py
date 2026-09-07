"""Test package init: sets safe defaults before any test module loads.

``os.environ.setdefault`` only fills in a value when nothing set it already, so an explicit outer
value (e.g. from ``tests/conftest.py`` honouring ``pytest --live-model``, or a value the shell
already exported) still wins.

These two lines are what stops a mis-written test from launching a real coding agent or spending
paid model tokens: ``LOCKEDIN_AGENT_TURNS=off`` is honoured by ``scientist_cli.AgentRunner.tick()``,
which returns immediately — no heartbeat, no job start, no process spawn — whenever it is set, and
``LOCKEDIN_LIVE_MODEL`` is left unset so anything gated behind it (see
``tests/_fixtures.py:live_model_test``) is skipped unless a caller opts in.
"""
import os

os.environ.setdefault("LOCKEDIN_AGENT_TURNS", "off")
