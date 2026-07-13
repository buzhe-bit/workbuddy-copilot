#!/usr/bin/env python3
"""Fail closed unless the release Python contract is satisfied."""
from __future__ import annotations

import sys
from typing import Sequence


MIN_VERSION = (3, 13)
MAX_VERSION_EXCLUSIVE = (3, 14)
REQUIRED_RANGE = ">=3.13,<3.14"


def supports_version(version: Sequence[int]) -> bool:
    current = tuple(version[:2])
    return MIN_VERSION <= current < MAX_VERSION_EXCLUSIVE


def main() -> int:
    current = sys.version_info[:3]
    if supports_version(current):
        print(f"Python {current[0]}.{current[1]}.{current[2]} satisfies {REQUIRED_RANGE}")
        return 0
    print(
        f"BLOCKED: Python {current[0]}.{current[1]}.{current[2]} does not satisfy {REQUIRED_RANGE}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
