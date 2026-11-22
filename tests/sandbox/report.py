"""Check the line and branch thresholds independently."""

import json
import sys
from pathlib import Path
from typing import Any

MINIMUM = 95


def main(path: Path) -> int:
    totals: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))["totals"]
    lines = totals["covered_lines"] * 100 / totals["num_statements"]
    branches = totals["covered_branches"] * 100 / totals["num_branches"]
    print(
        f"Lines: {lines:.2f}% | Branches: {branches:.2f}% | Required: {MINIMUM}% each"
    )
    return int(min(lines, branches) < MINIMUM)


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
