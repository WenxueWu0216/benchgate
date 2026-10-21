import os
from pathlib import Path
import subprocess

import pytest

REPO = Path(__file__).resolve().parents[1]
BUILD = REPO / "workloads" / "build"
BINARY = BUILD / "benchgate_workloads"


@pytest.fixture(scope="session")
def binary():
    """Build the C++ workloads once per session if needed."""
    if not BINARY.exists():
        subprocess.run(["cmake", "-S", str(REPO / "workloads"), "-B", str(BUILD),
                        "-DCMAKE_BUILD_TYPE=Release"], check=True, capture_output=True)
        subprocess.run(["cmake", "--build", str(BUILD), "-j", str(os.cpu_count() or 2)],
                       check=True, capture_output=True)
    return str(BINARY)
