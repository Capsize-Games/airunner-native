"""Regression test for this package's own install-isolation requirement.

Ported from services/tests/test_native_launcher_isolation.py in
Capsize-Games/airunner when native/ moved to this repository (issue
#2196). ``launcher.py``'s app-specific imports (``airunner.components...``,
``airunner.main``, the headless server's ``main``) are all function-scoped,
deferred until the launcher actually runs -- not hard install-time
dependencies. This package's own install_requires reflects that
(``airunner-common`` only; ``airunner``/``airunner-services`` are extras,
see setup.py), and this test pins the corresponding runtime property:
the module itself must import with neither present.
"""

from __future__ import annotations

import importlib
import sys

import pytest


def test_launcher_imports_without_airunner_or_services() -> None:
    """``airunner_native.launcher`` must import with only airunner_common."""
    assert "airunner" not in sys.modules
    assert "airunner_services" not in sys.modules

    importlib.import_module("airunner_native.launcher")

    assert "airunner_services" not in sys.modules, (
        "importing the launcher pulled in airunner_services"
    )
    assert "airunner" not in sys.modules, (
        "importing the launcher pulled in airunner"
    )
    with pytest.raises(ImportError):
        importlib.import_module("airunner")
    with pytest.raises(ImportError):
        importlib.import_module("airunner_services")
