"""Convert a flat CSV object listing into the inventory JSON the analyzer reads.

Required columns: key, size_bytes, tier, last_modified.
Optional columns: bucket (otherwise --bucket names the single bucket), previous_version (true/false).
Prices and the as-of date come from the command line, never from the CSV. A listing
carries no lifecycle rules, so every bucket comes out with none.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys

from . import model

REQUIRED = ["key", "size_bytes", "tier", "last_modified"]
TRUE, FALSE = {"true", "yes", "1"}, {"false", "no", "0", ""}


class ImportErrors(model.ModelError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(errors))


def parse_prices(text: str) -> dict:
    out = {}
    for part in text.split(","):
        name, _, val = part.partition("=")
        name = name.strip()
        if name not in model.TIERS:
            raise ImportErrors([f"--prices: unknown tier {name!r} (use {', '.join(model.TIERS)})"])
        try:
            out[name] = float(val)
        except ValueError:
            raise ImportErrors([f"--prices: bad number for {name}: {val!r}"]) from None
    missing = [t for t in model.TIERS if t not in out]
    if missing:
        raise ImportErrors([f"--prices: missing {', '.join(missing)}"])
    return out


def rows_to_doc(text: str, as_of: str, prices: dict, bucket: str | None = None) -> dict:
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        raise ImportErrors([f"missing column(s): {', '.join(missing)}"])
    if "bucket" not in header and not bucket:
        raise ImportErrors(["no 'bucket' column and no --bucket given"])
    reader.fieldnames = header
    buckets: dict[str, dict] = {}
    errors, seen = [], {}
    for n, row in enumerate(reader, start=2):  # row 1 is the header
        if None in row:
            errors.append(f"row {n}: more fields than columns")
            continue
        v = {k: (x or "").strip() for k, x in row.items() if k}
        if not any(v.values()):
            continue
        name = v.get("bucket") or bucket
        if not name:
            errors.append(f"row {n}: bucket is empty")
            continue
        if not v["key"]:
            errors.append(f"row {n}: key is empty")
            continue
        try:
            size = int(v["size_bytes"])
        except ValueError:
            errors.append(f"row {n}: size_bytes must be a whole number")
            continue
        pv = v.get("previous_version", "").lower()
        if pv not in TRUE | FALSE:
            errors.append(f"row {n}: previous_version must be true or false")
            continue
        ident = (name, v["key"], pv in TRUE)
        if ident in seen:
            errors.append(f"row {n}: duplicate object {v['key']!r} in bucket {name!r} (first at row {seen[ident]})")
            continue
        seen[ident] = n
        o = {"key": v["key"], "size_bytes": size, "tier": v["tier"].lower(), "last_modified": v["last_modified"]}
        if pv in TRUE:
            o["previous_version"] = True
        b = buckets.setdefault(name, {"name": name, "versioning": False, "objects": []})
        b["objects"].append(o)
        b["versioning"] = b["versioning"] or pv in TRUE
        # validate this row alone so errors carry the CSV row number
        try:
            model.load({"as_of": as_of, "prices": prices, "buckets": [{"name": name, "objects": [o]}]})
        except model.ModelError as e:
            errors.append(f"row {n}: {e}")
    if errors:
        raise ImportErrors(errors)
    if not buckets:
        raise ImportErrors(["no data rows"])
    return {"as_of": as_of, "prices": prices, "buckets": list(buckets.values())}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="os-import", description="Convert a flat object-listing CSV to the inventory JSON (synthetic/exported data).")
    ap.add_argument("csv")
    ap.add_argument("--as-of", required=True, help="date ages are measured from, YYYY-MM-DD")
    ap.add_argument("--prices", required=True, help="per GB-month, e.g. standard=0.025,infrequent_access=0.012,archive=0.003 (your own numbers)")
    ap.add_argument("--bucket", help="bucket name when the CSV has no bucket column")
    ap.add_argument("--output", help="write JSON here instead of stdout")
    a = ap.parse_args(argv)
    try:
        with open(a.csv, encoding="utf-8-sig", newline="") as f:
            doc = rows_to_doc(f.read(), a.as_of, parse_prices(a.prices), a.bucket)
        model.load(doc)
    except ImportErrors as e:
        for m in e.errors:
            print(f"error: {m}", file=sys.stderr)
        return 1
    except (OSError, model.ModelError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    out = json.dumps(doc, indent=1)
    if a.output:
        with open(a.output, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
