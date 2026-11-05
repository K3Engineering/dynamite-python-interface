import subprocess
import sys
from pathlib import Path


def test_stream_help_runs_from_a_checkout():
    root = Path(__file__).parents[1]
    result = subprocess.run(
        [sys.executable, "stream.py", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--conversion" in result.stdout
