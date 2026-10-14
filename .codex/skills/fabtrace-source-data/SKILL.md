---
name: fabtrace-source-data
description: Ingest, profile, validate, normalize, or document FabTrace's copied ETO semiconductor source tables. Use for source-data contracts and quality checks; do not use for synthetic provider behavior, orders, event transport, analytics, or UI work.
---

# FabTrace Source Data

Turn the immutable ETO CSV snapshots in `Data/` into trustworthy, traceable
reference data for the rest of FabTrace.

## Start safely

Read [AGENTS.md](../../../AGENTS.md), inspect the working tree, and confirm any
material choice such as storage format, normalized schema, validation framework,
or output location before implementing it. Never edit the copied source CSVs.

Use a CSV-aware parser because quoted descriptions contain commas and newlines.
Do not infer record counts from physical line counts.

## Preserve source meaning

Use IDs as keys and names as labels:

- `stages.stage_id`
- `inputs.input_id`
- `providers.provider_id`
- `provision.provider_id` and `provision.provided_id`
- Node IDs in `sequence.csv`

Account for these non-obvious source properties:

- `providers.csv` may contain multiple rows for one provider ID due to aliases.
- `provision.provided_id` may reference either an input (`N...`) or stage (`S...`).
- Blank `share_provided` is unknown/unquantified, not zero.
- Market-share totals may exceed 100 because of source rounding.
- Years vary across records; the files are not a uniform time series.
- Market-size fields are presentation strings until explicitly normalized.
- `goes_into` and `is_type_of` have different semantics.

Do not silently turn taxonomy edges into production dependencies or convert
research relationships into a quantitative BOM.

## Produce traceable derived data

Keep raw source values available alongside normalized fields. Record source
file, source row identity where practical, transformation version, and quality
status. Prefer explicit mappings and typed nullable fields over guessed values.

When resolving provider aliases, preserve every alias and the original rows.
When cleaning country codes, retain the raw code and document the chosen
standard. Quarantine or report invalid references rather than dropping them.

Profile and validate at least the relevant:

- Required fields and uniqueness expectations
- Referential integrity across all five tables
- Relationship-type exclusivity in `sequence.csv`
- Numeric share parsing and sensible bounds
- Year parsing and missingness
- Duplicate versus alias-row behavior
- Drift in row counts, columns, and categories

## Verify and hand off

Tests should use real edge cases from the source files without rewriting the
sources. Report parsed row counts, failed checks, quarantined records, and any
assumptions introduced by normalization.

Summarize the resulting reference-data contract and clearly distinguish source
facts, cleaning decisions, and synthetic enrichments. Do not expand into order
or simulation logic during a source-data task.
