"""Regression test for the native launcher's crash capture.

Ported from src/airunner/components/application/tests/test_crash_handler.py
in Capsize-Games/airunner when native/ moved to this repository (issue
#2196): that file also covered airunner_native.crash_handler (distinct
from airunner's own GUI crash handler -- two legitimate implementations
for two different processes, not a duplicate) by pointing a subprocess
at native/src via PYTHONPATH. That path no longer exists there.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_native_crash_handler_appends_to_gui_log(tmp_path: Path) -> None:
    """The native launcher crash handler lands tracebacks in gui.log."""
    script = (
        "import sys\n"
        "from airunner_native.crash_handler import install_crash_handlers\n"
        "install_crash_handlers(log_dir=sys.argv[1])\n"
        "def boom():\n"
        "    raise RuntimeError('native-crash-42')\n"
        "boom()\n"
    )
    env = os.environ.copy()
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0

    log_path = tmp_path / "gui.log"
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "RuntimeError: native-crash-42" in content
