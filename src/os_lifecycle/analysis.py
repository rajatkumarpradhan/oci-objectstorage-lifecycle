"""Findings and savings estimates.

Conservative rules (details in README):
- A rule is only considered if enabled. Prefix match is plain string prefix.
- STALE_DAYS_IA / STALE_DAYS_ARCHIVE: objects untouched this long on a pricier tier are candidates.
- Archive has a 90-day and Infrequent Access a 31-day minimum retention in this model; a rule that
  deletes sooner after a move than the minimum is flagged as an early-deletion risk.
- Savings use only the user-supplied price table, ignore retrieval fees and request costs,
  and are an upper bound for objects that are still read.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .model import MIN_RETENTION_DAYS, Inventory

GB = 1024 ** 3
STALE_DAYS_IA = 90
STALE_DAYS_ARCHIVE = 365
ABORT_AFTER_DAYS = 7
SEV = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Finding:
    rule: str
    severity: str
    bucket: str
    message: str
    evidence: dict

    def to_dict(self):
        return asdict(self)


def _covered(bucket, obj, action_kinds, target):
    return any(r.enabled and r.action in action_kinds and r.target == target and obj.key.startswith(r.prefix)
               for r in bucket.rules)


def analyze(inv: Inventory):
    out: list[Finding] = []
    totals = {"stale_ia_gb": 0.0, "stale_archive_gb": 0.0, "estimated_monthly_saving": 0.0}
    p = inv.prices
    for b in inv.buckets:
        live = [o for o in b.objects if not o.previous_version]
        prev = [o for o in b.objects if o.previous_version]
        enabled = [r for r in b.rules if r.enabled]
        if not enabled:
            out.append(Finding("OS001", "medium", b.name, "Bucket has no enabled lifecycle rules.",
                               {"objects": len(b.objects)}))

        # stale objects not covered by a tiering rule
        ia_gb = arch_gb = 0.0
        for o in live:
            age = (inv.as_of - o.last_modified).days
            if o.tier == "standard" and age >= STALE_DAYS_IA and not _covered(b, o, ("move_infrequent_access", "move_archive"), "objects"):
                if age >= STALE_DAYS_ARCHIVE:
                    arch_gb += o.size_bytes / GB
                else:
                    ia_gb += o.size_bytes / GB
            elif o.tier == "infrequent_access" and age >= STALE_DAYS_ARCHIVE and not _covered(b, o, ("move_archive",), "objects"):
                arch_gb += o.size_bytes / GB
        if ia_gb or arch_gb:
            saving = ia_gb * max(0.0, p["standard"] - p["infrequent_access"]) + \
                     arch_gb * max(0.0, p["standard"] - p["archive"])
            totals["stale_ia_gb"] += ia_gb
            totals["stale_archive_gb"] += arch_gb
            totals["estimated_monthly_saving"] += saving
            out.append(Finding("OS002", "medium", b.name,
                               f"{ia_gb:.2f} GB untouched for {STALE_DAYS_IA}+ days and {arch_gb:.2f} GB for {STALE_DAYS_ARCHIVE}+ days sit on a pricier tier with no tiering rule. "
                               f"Upper-bound saving {saving:.2f}/month in the supplied price units (ignores retrieval and request fees).",
                               {"candidate_ia_gb": round(ia_gb, 3), "candidate_archive_gb": round(arch_gb, 3),
                                "estimated_monthly_saving": round(saving, 2)}))

        # incomplete multipart uploads
        old = [u for u in b.uploads if (inv.as_of - u.initiated).days > ABORT_AFTER_DAYS]
        if old and not any(r.enabled and r.action == "abort_multipart" for r in b.rules):
            gb = sum(u.size_bytes for u in old) / GB
            out.append(Finding("OS003", "medium", b.name,
                               f"{len(old)} incomplete multipart upload(s) older than {ABORT_AFTER_DAYS} days ({gb:.2f} GB) and no abort rule.",
                               {"uploads": len(old), "gb": round(gb, 3)}))

        # previous versions
        if b.versioning and prev and not any(r.enabled and r.target == "previous_versions" and r.action == "delete" for r in b.rules):
            gb = sum(o.size_bytes for o in prev) / GB
            out.append(Finding("OS004", "medium", b.name,
                               f"Versioning is on with {len(prev)} previous version(s) ({gb:.2f} GB) and no rule deleting old versions.",
                               {"previous_versions": len(prev), "gb": round(gb, 3)}))
        if not b.versioning and prev:
            out.append(Finding("OS007", "low", b.name,
                               "Inventory lists previous versions but versioning is off in the export; check the export.",
                               {"previous_versions": len(prev)}))

        # early deletion risk and rule ordering
        by_prefix: dict[tuple[str, str], list] = {}
        for r in enabled:
            by_prefix.setdefault((r.prefix, r.target), []).append(r)
        for (prefix, target), rs in by_prefix.items():
            moves = [r for r in rs if r.action.startswith("move_")]
            dels = [r for r in rs if r.action == "delete"]
            for m in moves:
                tier = "archive" if m.action == "move_archive" else "infrequent_access"
                for d in dels:
                    if d.after_days - m.after_days < MIN_RETENTION_DAYS[tier]:
                        out.append(Finding("OS005", "high", b.name,
                                           f"Rule '{d.name}' deletes at day {d.after_days}, only {d.after_days - m.after_days} days after '{m.name}' moves to {tier} (minimum {MIN_RETENTION_DAYS[tier]}). Early-deletion charges may apply.",
                                           {"move_rule": m.name, "delete_rule": d.name, "min_days": MIN_RETENTION_DAYS[tier]}))
            ia = [r for r in moves if r.action == "move_infrequent_access"]
            ar = [r for r in moves if r.action == "move_archive"]
            for i in ia:
                for a in ar:
                    if a.after_days <= i.after_days:
                        out.append(Finding("OS006", "low", b.name,
                                           f"Rule '{a.name}' (archive at {a.after_days}d) fires no later than '{i.name}' (IA at {i.after_days}d); the IA step never applies.",
                                           {"ia_rule": i.name, "archive_rule": a.name}))
        # objects already past a delete rule's age (rule may not be applying); one finding per rule
        overdue: dict[str, list] = {}
        for o in live:
            age = (inv.as_of - o.last_modified).days
            for r in enabled:
                if r.action == "delete" and r.target == "objects" and o.key.startswith(r.prefix) and age > r.after_days + 30:
                    overdue.setdefault(r.name, []).append((o.key, age, r.after_days))
                    break
        for name, items in sorted(overdue.items()):
            oldest = max(items, key=lambda t: t[1])
            out.append(Finding("OS008", "low", b.name,
                               f"{len(items)} object(s) are more than 30 days past delete rule '{name}' ({items[0][2]}d); oldest {oldest[0]} at {oldest[1]} days. Check the rule is applying.",
                               {"rule": name, "objects": len(items), "oldest_age_days": oldest[1]}))
    out.sort(key=lambda f: (SEV[f.severity], f.rule, f.bucket))
    totals = {k: round(v, 3) for k, v in totals.items()}
    return out, totals


ROOT_PREFIX = "(root)"


def top_prefix(key: str) -> str:
    """First path segment of an object key, or '(root)' when the key has no '/'."""
    return key.split("/", 1)[0] + "/" if "/" in key else ROOT_PREFIX


def prefix_breakdown(inv: Inventory) -> dict:
    """Stale-data candidates grouped by bucket and top-level prefix (same rules as OS002).

    Totals across prefixes equal the OS002 totals; this only shows where the data sits.
    """
    p = inv.prices
    result: dict = {}
    for b in inv.buckets:
        groups: dict = {}
        for o in b.objects:
            if o.previous_version:
                continue
            age = (inv.as_of - o.last_modified).days
            ia = arch = 0.0
            if o.tier == "standard" and age >= STALE_DAYS_IA and not _covered(b, o, ("move_infrequent_access", "move_archive"), "objects"):
                if age >= STALE_DAYS_ARCHIVE:
                    arch = o.size_bytes / GB
                else:
                    ia = o.size_bytes / GB
            elif o.tier == "infrequent_access" and age >= STALE_DAYS_ARCHIVE and not _covered(b, o, ("move_archive",), "objects"):
                arch = o.size_bytes / GB
            if ia or arch:
                g = groups.setdefault(top_prefix(o.key), {"candidate_ia_gb": 0.0, "candidate_archive_gb": 0.0, "objects": 0})
                g["candidate_ia_gb"] += ia
                g["candidate_archive_gb"] += arch
                g["objects"] += 1
        for g in groups.values():
            g["estimated_monthly_saving"] = (g["candidate_ia_gb"] * max(0.0, p["standard"] - p["infrequent_access"])
                                             + g["candidate_archive_gb"] * max(0.0, p["standard"] - p["archive"]))
            for k in ("candidate_ia_gb", "candidate_archive_gb"):
                g[k] = round(g[k], 3)
            g["estimated_monthly_saving"] = round(g["estimated_monthly_saving"], 2)
        if groups:
            result[b.name] = dict(sorted(groups.items(), key=lambda kv: (-kv[1]["estimated_monthly_saving"], kv[0])))
    return result
