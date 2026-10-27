"""Exercise the build script's artifact checks without running the build."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "deploy/docker/scripts/build_mooncake_sglang.sh"


@pytest.mark.parametrize("missing", [None, "symbol", "library", "routing"])
def test_artifact_checks_with_large_output(tmp_path, missing):
    # A match near the start must not close the pipe while printf still writes.
    filler = "unused_symbol_or_string\n" * 100000
    outputs = {
        "SYMS": ("ibv_reg_dmabuf_mr\n" if missing != "symbol" else "") + filler,
        "LIBS": ("libHSA-runtime64.so\n" if missing != "library" else "") + filler,
        "STRINGS": ("MC_DISABLE_HIP\n" if missing != "routing" else "") + filler,
    }
    env = os.environ.copy()
    for key, value in outputs.items():
        path = tmp_path / key
        path.write_text(value)
        env[key] = str(path)
    source = SCRIPT.read_text()
    checks = source.split("# Artifact checks:", 1)[1].split('features="', 1)[0]
    # The initial comment continues after the split marker.
    checks = checks.split("\n", 1)[1]
    command = (
        """
set -euo pipefail
DEST=yihou-test-artifact.so
nm() { cat "$SYMS"; }
ldd() { cat "$LIBS"; }
strings() { cat "$STRINGS"; }
"""
        + checks
    )
    result = subprocess.run(
        ["bash", "-c", command], env=env, capture_output=True, text=True, timeout=10
    )
    if missing is None:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        expected = {
            "symbol": "dma-buf registration is absent",
            "library": "does not link hsa-runtime64",
            "routing": "cross-host HIP locality routing is absent",
        }
        assert expected[missing] in result.stderr
