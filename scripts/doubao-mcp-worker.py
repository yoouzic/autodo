"""Absolute-path Doubao connector; fixed provider, independent of the host cwd."""
from pathlib import Path
import sys


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from dispatcher.workbuddy_mcp import main


if __name__ == '__main__':
    if any(value == '--provider' or value.startswith('--provider=') for value in sys.argv[1:]):
        print('This connector is fixed to the doubao provider.', file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(['--provider', 'doubao', *sys.argv[1:]], exit_on_transport_failure=True))
