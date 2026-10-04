"""Load and validate a simplified bucket inventory.

Price table values are user-supplied per GB-month numbers. The bundled example uses
ILLUSTRATIVE numbers, not real OCI prices.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime

TIERS = ("standard", "infrequent_access", "archive")
ACTIONS = ("move_infrequent_access", "move_archive", "delete", "abort_multipart")
TARGETS = ("objects", "previous_versions", "multipart")
MIN_RETENTION_DAYS = {"standard": 0, "infrequent_access": 31, "archive": 90}


class ModelError(ValueError):
    pass


@dataclass
class Obj:
    key: str
    size_bytes: int
    tier: str
    last_modified: date
    previous_version: bool = False


@dataclass
class Upload:
    key: str
    initiated: date
    size_bytes: int


@dataclass
class Rule:
    name: str
    action: str
    after_days: int
    target: str = "objects"
    prefix: str = ""
    enabled: bool = True


@dataclass
class Bucket:
    name: str
    versioning: bool
    objects: list[Obj] = field(default_factory=list)
    uploads: list[Upload] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)


@dataclass
class Inventory:
    as_of: date
    prices: dict[str, float]
    buckets: list[Bucket]


def _date(v, what) -> date:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
    except ValueError:
        raise ModelError(f"{what}: bad date {v!r}") from None


def _size(v, what) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise ModelError(f"{what}: size must be a non-negative integer")
    return v


def load(data: dict) -> Inventory:
    try:
        prices = {k: float(data["prices"][k]) for k in TIERS}
        if any(p < 0 for p in prices.values()):
            raise ModelError("prices must be non-negative")
        as_of = _date(data["as_of"], "as_of")
        buckets, seen = [], set()
        for b in data["buckets"]:
            if b["name"] in seen:
                raise ModelError(f"duplicate bucket {b['name']}")
            seen.add(b["name"])
            bk = Bucket(b["name"], bool(b.get("versioning", False)))
            for o in b.get("objects", []):
                if o["tier"] not in TIERS:
                    raise ModelError(f"{b['name']}/{o['key']}: unknown tier {o['tier']!r}")
                lm = _date(o["last_modified"], f"{b['name']}/{o['key']}")
                if lm > as_of:
                    raise ModelError(f"{b['name']}/{o['key']}: last_modified is after as_of")
                bk.objects.append(Obj(o["key"], _size(o["size_bytes"], o["key"]), o["tier"], lm,
                                      bool(o.get("previous_version", False))))
            for u in b.get("multipart_uploads", []):
                bk.uploads.append(Upload(u["key"], _date(u["initiated"], u["key"]), _size(u.get("size_bytes", 0), u["key"])))
            for r in b.get("lifecycle_rules", []):
                if r["action"] not in ACTIONS:
                    raise ModelError(f"{b['name']}: unknown action {r['action']!r}")
                tgt = r.get("target", "multipart" if r["action"] == "abort_multipart" else "objects")
                if tgt not in TARGETS:
                    raise ModelError(f"{b['name']}: unknown target {tgt!r}")
                if r["action"] == "abort_multipart" and tgt != "multipart":
                    raise ModelError(f"{b['name']}: abort_multipart must target multipart")
                days = r["after_days"]
                if not isinstance(days, int) or isinstance(days, bool) or days < 1:
                    raise ModelError(f"{b['name']}/{r['name']}: after_days must be an integer >= 1")
                bk.rules.append(Rule(r["name"], r["action"], days, tgt, r.get("prefix", ""), bool(r.get("enabled", True))))
            buckets.append(bk)
    except (KeyError, TypeError) as e:
        raise ModelError(f"invalid inventory: {e!r}") from e
    return Inventory(as_of, prices, buckets)


def load_file(path: str) -> Inventory:
    with open(path, encoding="utf-8") as f:
        return load(json.load(f))
