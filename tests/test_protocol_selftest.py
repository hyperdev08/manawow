"""End-to-end: runs tools/protocol_selftest.py (real auth + world servers, fake clients)."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.slow
def test_protocol_selftest_passes():
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "protocol_selftest.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "[5/5] PASS" in output, output
