#!/usr/bin/env python
"""Thin wrapper so an agent can run the ICT engine from the skill folder.

Usage:
    python skills/ict-analysis/scripts/analyze.py plan --csv ... --daily-csv ...
"""
from __future__ import annotations

import sys

from ict_trader.cli import main

if __name__ == "__main__":
    main(sys.argv[1:])
