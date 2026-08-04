"""Export the frozen synthetic bars for a no-source-access oracle exercise."""

from __future__ import annotations

import csv
from pathlib import Path

from app.proof_kernel import fixture_bars


def main() -> None:
    target = Path(__file__).parents[1] / "independent_oracle" / "spec" / "bars.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("index", "timestamp_utc", "close"))
        for bar in fixture_bars():
            writer.writerow((bar.index, bar.ts.isoformat(), str(bar.close)))
    print(target)


if __name__ == "__main__":
    main()
