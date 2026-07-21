"""
Silver 1:1 flatten mirror: every non-empty Bronze object -> one flat Parquet.

Guarantees complete bronze coverage (nothing left behind). These are the "as-is,
flattened" tables; the conformed/joined models (dim_*, fact_*) from build_silver_gold.py
live alongside at silver/ root. Output:

    data/silver/flat/sp__<List>.parquet      (from bronze/bms/<module>/<List>)
    data/silver/flat/opms__<obj>.parquet     (from bronze/opms/<obj>)

Flattening: nested dicts -> dotted columns (one level); lists/dicts/mixed-type
columns -> JSON string (keeps Parquet happy). For SharePoint the business payload
under `fields` is unwrapped.

Run:
    python build_silver_flat.py            # build + upload to silver/flat/
    python build_silver_flat.py --no-upload
"""

import argparse
import glob
import json
from pathlib import Path

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent.parent
BRONZE = DATA_DIR / "bronze"
FLAT = DATA_DIR / "silver" / "flat"
FLAT.mkdir(parents=True, exist_ok=True)


# Lists whose oversized blob columns must be stripped before flattening, or
# pd.json_normalize explodes memory. FDD-FormInbox is ~955MB of per-row form
# Schema/Images/Data payloads (Schema is repeated form-layout boilerplate, ~75%
# of the bulk). Bronze keeps the full raw untouched; we drop here only for the
# flat mirror. The rich Data/Schema content is handled separately (RAG layer).
HEAVY_DROP = {
    "FDD-FormInbox": {"Schema", "Images", "Data"},
}


def read_ndjson(path, unwrap_fields, drop=None):
    out = []
    for line in open(path, encoding="utf-8"):
        rec = json.loads(line)
        if unwrap_fields and isinstance(rec.get("fields"), dict):
            rec = rec["fields"]
        if drop:
            for k in drop:
                rec.pop(k, None)
        out.append(rec)
    return out


def safe_df(records):
    """Flatten one level; stringify any list/dict/mixed-type column so Parquet writes cleanly."""
    df = pd.json_normalize(records, max_level=1)
    for c in df.columns:
        nn = df[c].dropna()
        types = {type(v).__name__ for v in nn}
        if (types & {"list", "dict"}) or len(types) > 1:
            df[c] = df[c].apply(
                lambda v: None if v is None
                else (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else str(v))
            )
    return df


def build_streaming(path, unwrap_fields, drop, dest, batch=2000):
    """Memory-bounded build for HEAVY objects (e.g. FDD-FormInbox ~955MB): two passes —
    pass 1 collects the column union (keys only, tiny memory), pass 2 streams the file in
    batches and appends each batch as a Parquet row-group. Never holds the whole file or a
    full DataFrame in memory. All columns are written as strings for a stable schema."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    def norm(line):
        rec = json.loads(line)
        if unwrap_fields and isinstance(rec.get("fields"), dict):
            rec = rec["fields"]
        if drop:
            for k in drop:
                rec.pop(k, None)
        return rec

    cols = set()
    for line in open(path, encoding="utf-8"):
        cols.update(norm(line).keys())
    cols = sorted(cols) or ["_no_data"]
    schema = pa.schema([(c, pa.string()) for c in cols])

    def stringify(v):
        if v is None:
            return None
        return json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else str(v)

    writer = pq.ParquetWriter(dest, schema)
    n, buf = 0, []

    def flush():
        if not buf:
            return
        df = pd.DataFrame(buf).reindex(columns=cols)
        for c in cols:
            df[c] = df[c].map(stringify)
        writer.write_table(pa.Table.from_pandas(df, schema=schema, preserve_index=False))
        buf.clear()

    for line in open(path, encoding="utf-8"):
        buf.append(norm(line))
        n += 1
        if len(buf) >= batch:
            flush()
    flush()
    writer.close()
    return n, len(cols)


def discover():
    """Yield (source_tag, object_name, ndjson_path, unwrap_fields) for every non-empty bronze object."""
    for f in glob.glob(str(BRONZE / "bms" / "*" / "*" / "ingest_date=*" / "items.ndjson")):
        obj = Path(f).parents[1].name
        yield "sp", obj, f, True
    for f in glob.glob(str(BRONZE / "opms" / "*" / "ingest_date=*" / "items.ndjson")):
        obj = Path(f).parents[1].name
        yield "opms", obj, f, False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-upload", action="store_true")
    args = ap.parse_args()

    built = []
    empty_schema = []
    for src, obj, path, unwrap in sorted(discover(), key=lambda x: (x[0], x[1])):
        name = f"{src}__{obj}.parquet"
        if obj in HEAVY_DROP:
            # huge object -> stream in batches so it never OOMs (the lulu-refresh OOM fix)
            rows, ncols = build_streaming(path, unwrap, HEAVY_DROP.get(obj), FLAT / name)
            built.append((name, rows, ncols))
            continue
        recs = read_ndjson(path, unwrap, drop=HEAVY_DROP.get(obj))
        if recs:
            df = safe_df(recs)
            df.to_parquet(FLAT / name, index=False)
            built.append((name, len(df), len(df.columns)))
        else:
            # empty object -> schema-only 0-row table (columns from _columns.json if present)
            cols = []
            cj = Path(path).parent / "_columns.json"
            if cj.exists():
                try:
                    cols = [c.get("name") for c in json.loads(cj.read_text(encoding="utf-8")) if c.get("name")]
                except Exception:
                    cols = []
            if not cols:
                cols = ["_no_data"]
            pd.DataFrame(columns=cols).astype("string").to_parquet(FLAT / name, index=False)
            empty_schema.append((name, len(cols)))

    print("=== SILVER/flat — full bronze mirror ===")
    for name, rows, cols in built:
        print(f"  {name:42s} {rows:7d} rows  {cols:3d} cols")
    print("  --- empty (schema-only, 0 rows) ---")
    for name, cols in empty_schema:
        print(f"  {name:42s}       0 rows  {cols:3d} cols")
    print(f"\nbuilt {len(built)} data tables + {len(empty_schema)} empty schema tables = {len(built)+len(empty_schema)} total")

    if not args.no_upload:
        print("=== UPLOAD -> silver/flat/ ===")
        import importlib.util
        spec = importlib.util.spec_from_file_location("u", str(SCRIPT_DIR / "upload_to_blob.py"))
        u = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(u)
        from azure.storage.blob import ContentSettings
        cc = u.get_service_client().get_container_client(u.CONTAINER)
        n = 0
        for p in sorted(FLAT.glob("*.parquet")):
            with open(p, "rb") as fh:
                cc.upload_blob(f"silver/flat/{p.name}", fh, overwrite=True,
                               content_settings=ContentSettings(content_type="application/octet-stream"))
            n += 1
            if n % 20 == 0 or n == len(built):
                print(f"  uploaded {n}/{len(built)}")
        print(f"uploaded {n} flat tables to silver/flat/")

    print("\nDONE.")


if __name__ == "__main__":
    main()
