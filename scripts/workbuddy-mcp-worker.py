"""Launch the dispatcher MCP worker independently of the host application's cwd.

The installed connector may change its working directory when a task starts.
Resolve only this script's own source tree; do not change global Python settings.
"""
from pathlib import Path
import sys


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from dispatcher.workbuddy_mcp import main


if __name__ == '__main__':
    raise SystemExit(main(exit_on_transport_failure=True))
