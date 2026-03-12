#!/usr/bin/env python3
"""
Companest CLI entry point.

Allows running the CLI as:
    python -m companest validate .companest/config.md
    python -m companest serve
    python -m companest fleet status
    python -m companest job submit "Analyze this codebase"
"""

from .cli import main

if __name__ == "__main__":
    main()
