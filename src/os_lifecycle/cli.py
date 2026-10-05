from __future__ import annotations

import argparse
import json
import sys

from . import analysis, model


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="os-lifecycle", description="Offline Object Storage lifecycle review (synthetic/exported inventory).")
    ap.add_argument("input")
    ap.add_argument("--output")
    ap.add_argument("--fail-on-high", action="store_true")
    ap.add_argument("--by-prefix", action="store_true", help="also print stale-data candidates grouped by top-level prefix")
    a = ap.parse_args(argv)
    try:
        inv = model.load_file(a.input)
    except (OSError, json.JSONDecodeError, model.ModelError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    fs, totals = analysis.analyze(inv)
    for f in fs:
        print(f"[{f.severity.upper():6}] {f.rule} {f.bucket}: {f.message}")
    print(f"{len(fs)} finding(s). Upper-bound saving {totals['estimated_monthly_saving']:.2f}/month (illustrative price units).")
    breakdown = analysis.prefix_breakdown(inv)
    if a.by_prefix:
        for bucket, groups in breakdown.items():
            print(f"{bucket}:")
            for pre, g in groups.items():
                print(f"  {pre:20} IA {g['candidate_ia_gb']:8.2f} GB  archive {g['candidate_archive_gb']:8.2f} GB  saving {g['estimated_monthly_saving']:8.2f}")
    if a.output:
        with open(a.output, "w", encoding="utf-8") as f:
            json.dump({"as_of": inv.as_of.isoformat(), "totals": totals, "findings": [x.to_dict() for x in fs], "by_prefix": breakdown,
                       "disclaimer": "Estimates use the supplied price table and ignore retrieval/request fees."}, f, indent=1)
    return 2 if a.fail_on_high and any(f.severity == "high" for f in fs) else 0


if __name__ == "__main__":
    sys.exit(main())
