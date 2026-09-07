"""Allow `python -m seninfo ...` without installation."""

import sys

from seninfo.cli import main

if __name__ == "__main__":
    sys.exit(main())
