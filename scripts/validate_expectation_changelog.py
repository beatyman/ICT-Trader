#!/usr/bin/env python
"""Validate EXPECTATION_CHANGELOG.md entries."""
from __future__ import annotations

import re
import sys
from pathlib import Path

FIELDS = ["change_id", "rule_id", "old_expected", "new_expected",
          "evidence", "classification", "confirmed_by", "regression_case"]


def validate(text: str) -> list[str]:
    errors: list[str] = []
    blocks = re.split(r"(?m)^## ", text)
    for block in blocks:
        head, _, body = block.partition("\n")
        if not head.startswith("EC-") or "YYYYMMDD" in head:
            continue
        values: dict[str, str] = {}
        for line in body.splitlines():
            m = re.match(r"\s*-\s*([a-z_]+):\s*(.*)$", line)
            if m:
                values[m.group(1)] = m.group(2).strip()
        for field in FIELDS:
            val = values.get(field, "")
            if not val or val.upper() in {"TODO", "TBD", "N/A"}:
                errors.append(f"{head}: missing/empty {field}")
        if values.get("classification") not in {"expectation_error", "implementation_error", "data_issue"}:
            errors.append(f"{head}: invalid classification")
    return errors


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/EXPECTATION_CHANGELOG.md")
    errors = validate(path.read_text(encoding="utf-8"))
    if errors:
        for e in errors:
            print(e)
        raise SystemExit(1)
    print(f"ok: {path}")


if __name__ == "__main__":
    main()
