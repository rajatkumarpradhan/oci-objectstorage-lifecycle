# oci-objectstorage-lifecycle

Offline review of Object Storage lifecycle rules and tiering from an exported bucket inventory: which data is sitting on an expensive tier with no rule to move it, which rules would trigger early-deletion charges, and where incomplete uploads and old versions pile up.

**Scope, stated plainly.** Portfolio project. It reads a simplified JSON inventory. The bundled data is synthetic and the price table in `examples/synthetic.json` is **illustrative, not real OCI pricing**; supply your own numbers. It has not been run against a real tenancy, calls no OCI API, and spends nothing. Savings are upper-bound estimates.

## Usage

```
python -m pip install -e .
os-lifecycle examples/synthetic.json --output report.json
os-lifecycle examples/synthetic.json --by-prefix          # stale data grouped by top-level prefix
os-lifecycle examples/synthetic.json --fail-on-high      # exit code 2 on any high finding
python -m unittest discover -s tests -v
```

The input has `as_of` (the date ages are measured from, so results are reproducible), a `prices` table (per GB-month for `standard`, `infrequent_access`, `archive`), and buckets with `objects`, `multipart_uploads` and `lifecycle_rules`.

## Rules

| Rule | Severity | Meaning |
|---|---|---|
| OS001 | medium | Bucket has no enabled lifecycle rules |
| OS002 | medium | Objects untouched 90+ days (365+ for Archive) on a pricier tier with no tiering rule; upper-bound saving |
| OS003 | medium | Incomplete multipart uploads older than 7 days and no abort rule |
| OS004 | medium | Versioning on, previous versions present, no rule deleting them |
| OS005 | high | A delete rule fires sooner after a move than the tier's minimum retention (Archive 90 days, Infrequent Access 31 days): early-deletion charges may apply |
| OS006 | low | An archive rule fires no later than the Infrequent Access rule, so the IA step never applies |
| OS007 | low | Previous versions in the export but versioning off: check the export |
| OS008 | low | Objects more than 30 days past a delete rule's age: check the rule is applying |

Rules match by plain string prefix; disabled rules are ignored. The 31 and 90 day minimums are the model's assumptions: check current Oracle documentation before relying on them.

`--by-prefix` prints where the OS002 candidates sit, grouped by the first path segment of the key (`2023/`, or `(root)` for keys with no `/`), largest saving first. The JSON report always includes the same breakdown under `by_prefix`. Totals across prefixes equal the OS002 totals (a test checks this); findings and totals are otherwise unchanged. Only the first path segment is used, so nested folders roll up.

## Limits

- Retrieval fees, request costs, minimum object sizes, replication and cross-region copies are not modelled, so savings are an upper bound and overstate gains for data that is still read.
- "Last modified" stands in for access; Object Storage access tracking is not modelled.
- Rule actions are simplified (move to IA, move to Archive, delete, abort multipart). Object-name filters other than a prefix are not supported.
- One snapshot in time; no growth forecast.
- No exporter from a real bucket listing exists; only synthetic inventories were tested.

## Tests

34 unit tests cover validation, each rule and its boundary (for example exactly 90 days after an Archive move), prefix scoping, disabled rules, the saving arithmetic, never reporting a negative saving, and the CLI. CI runs on Python 3.10, 3.11 and 3.12.

## License

MIT
