#!/usr/bin/env python3
"""Run without installation: python3 closeout.py --help."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "skills" / "project-closeout" / "scripts"))
from project_closeout.cli import main

if __name__ == "__main__":
    sys.exit(main())
