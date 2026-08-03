"""
compare_gold_backends.py — parity harness for GOLD_BACKEND cutover.

Mirrors gold from BOTH backends into scratch dirs and compares, per table:
  * schema (column names + dtypes)
  * row count
  * null count per column
  * key business metrics (sum of numeric columns — catches double-counting)
  * min/max of date-like columns

Run when both backends are configured (needs blob conn string AND ONELAKE_* env):
    python compare_gold_backends.py
Exit 0 = parity OK, exit 1 = differences found (report printed + JSON written).

This is the acceptance gate before flipping GOLD_BACKEND=onelake in production,
and the daily dual-run check while old and new pipelines run in parallel.
"""
import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

from gold_repository import get_repository

REPORT_PATH = Path(__file__).resolve().parent / "logs" / "gold_parity_report.json"


def snapshot(df: pd.DataFrame):
    num = df.select_dtypes("number")
    date_like = [c for c in df.columns if "date" in c.lower() or c.lower().endswith("_at")]
    return {
        "rows": len(df),
        "schema": {c: str(t) for c, t in df.dtypes.items()},
        "nulls": {c: int(df[c].isna().sum()) for c in df.columns},
        "numeric_sums": {c: round(float(num[c].sum()), 4) for c in num.columns},
        "date_ranges": {c: [str(df[c].min()), str(df[c].max())] for c in date_like if len(df)},
    }


def compare_table(name, dir_a, dir_b):
    diffs = []
    a, b = dir_a / f"{name}.parquet", dir_b / f"{name}.parquet"
    if not a.exists() or not b.exists():
        return [f"presence mismatch: {'A missing' if not a.exists() else 'B missing'}"]
    sa, sb = snapshot(pd.read_parquet(a)), snapshot(pd.read_parquet(b))
    for key in ("rows", "schema", "nulls", "numeric_sums", "date_ranges"):
        if sa[key] != sb[key]:
            diffs.append({key: {"azure_blob": sa[key], "onelake": sb[key]}})
    return diffs


def main():
    blob, onelake = get_repository("azure_blob"), get_repository("onelake")
    for repo in (blob, onelake):
        if not repo.health_check():
            print(f"[ABORT] backend '{repo.name}' unhealthy/unconfigured — nothing compared")
            return 2

    with tempfile.TemporaryDirectory() as tmp:
        dir_a, dir_b = Path(tmp) / "blob", Path(tmp) / "onelake"
        na, nb = blob.sync_gold(dir_a), onelake.sync_gold(dir_b)
        print(f"mirrored: azure_blob={na} files (version {blob.get_version()}), "
              f"onelake={nb} files (version {onelake.get_version()})")

        tables = sorted(set(blob.list_tables()) | set(onelake.list_tables()))
        report, bad = {}, 0
        for t in tables:
            diffs = compare_table(t, dir_a, dir_b)
            report[t] = diffs or "OK"
            status = "OK " if not diffs else "DIFF"
            if diffs:
                bad += 1
            print(f"  [{status}] {t}" + ("" if not diffs else f" -> {json.dumps(diffs)[:200]}"))

    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\n{len(tables) - bad}/{len(tables)} tables at parity. Report -> {REPORT_PATH}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
