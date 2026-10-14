# FabTrace

FabTrace is an event-driven semiconductor supply-chain digital twin and control
tower. The project uses the copied ETO Advanced Semiconductor Supply Chain data
as immutable reference data and will add explicitly synthetic operational data
for orders, provider activity, delivery forecasts, risk, quality, and cost.

## Current milestone

The current implementation loads and validates the five source tables in
`Data/` and runs happy-path, explicitly configured failure, and reproducible
seeded-stochastic provider scenarios. It can ingest the resulting provider
updates into append-only SQLite raw storage with replay, deduplication, and
quarantine, extract and evaluate versioned structured claims, and reconcile
those claims into historical and current order state. It computes reproducible
operational metrics and serves them through a read-only API and dashboard. It
also provides a visual, versioned product dependency editor and expands a
published product into immutable provider-order plans with expected delivery
and a critical path. Plan-driven simulation executes those provider orders as
concurrent, dependency-aware actors. It does not modify the source files.
The complete plan path can also be executed by one checkpointed local pipeline
command whose append-only run history is visible on the customer-order page.

## Development setup

FabTrace requires Python 3.12 or newer and uses `uv` for environment and
dependency management.

```bash
uv sync --extra dev
uv run fabtrace data validate --data-dir Data
uv run pytest
```

Run the approved provider scenario:

```bash
uv run fabtrace simulate \
  --scenario .fabtrace/config/scenarios/happy_path.json \
  --data-dir Data \
  --output-dir .fabtrace/runs
```

Run the deterministic equipment-failure scenario:

```bash
uv run fabtrace simulate \
  --scenario .fabtrace/config/scenarios/equipment_failure.json \
  --data-dir Data \
  --output-dir .fabtrace/runs
```

The failure scenario pauses production after 25 wafers, enters `BLOCKED` for two
simulated days, resumes production, and revises delivery from January 13 to
January 15, 2026.

Run the seeded stochastic disruption scenario:

```bash
uv run fabtrace simulate \
  --scenario .fabtrace/config/scenarios/stochastic_disruption.json \
  --data-dir Data \
  --output-dir .fabtrace/runs
```

The stochastic scenario uses an order-local pseudorandom stream derived from
the scenario seed and stable order identities. With the committed seed of 42,
the provider fails on its second production day, pauses for three days, and
revises delivery from January 13 to January 16, 2026. Its intentionally high
failure probability is a synthetic teaching assumption, not a claim about the
source provider.

The simulator refuses to overwrite an existing run. Remove or archive an old
run intentionally before repeating the same scenario and seed.

### Plan-driven multi-provider simulation

Stage 11 consumes an immutable planner artifact and executes one lightweight
SimPy actor per provider order on a shared virtual clock. Independent orders run
concurrently. A dependent order waits until both its planned release time and
all upstream `DELIVERED` events have occurred, so an upstream disruption shifts
downstream execution causally.

Product nodes intended for execution must have an ETO reference, and their
selected provider IDs must be source provider IDs with matching synthetic
profiles and capability units in the execution scenario. FabTrace rejects
missing, ambiguous, or incompatible mappings rather than inventing them. The
committed example scenario contains profiles for P208 and P226 providing N26 in
`wafer` units; add explicit profiles before using other mappings.

After creating a new plan in Product Designer, run:

```bash
uv run fabtrace simulate-plan \
  --plan .fabtrace/plans/ORDER-0001/plan.json \
  --scenario .fabtrace/config/plan-scenarios/default.json \
  --data-dir Data \
  --output-dir .fabtrace/runs
```

The run directory is named
`<order-id>-<execution-scenario-id>-seed-<seed>` and is never overwritten. It
contains:

- `source_plan.json`: the exact immutable planner input.
- `effective_config.json`: resolved ETO identities, countries, synthetic
  provider profiles, capabilities, orders, dependencies, and plan fingerprint.
- `run_manifest.json`: command, source-table hashes, plan hash, versions, and
  record counts.
- `ground_truth.jsonl`: private chronological operational truth across actors.
- `provider_updates.jsonl`: chronological natural-language messages linked to
  their truth events.

Purchase and work orders retain the existing lifecycle through `DELIVERED`.
For a work order, delivery means that completed output was handed to the next
production step rather than customer freight. Random streams are derived from
the scenario, provider profile, and provider-order ID, so unrelated actors do
not change one another's stochastic draws.

The existing raw ingestion stage accepts these provider messages unchanged.
The default `rules-v2` extractor understands full plan-generated purchase- and
work-order IDs plus work-output handoff wording. `rules-v1` remains available
as an append-only legacy baseline.

## Simulation configuration and output

Committed user configuration lives under `.fabtrace/config/`. At startup,
FabTrace combines ETO source facts, a synthetic provider profile, and scenario
orders into a deterministic effective configuration.

Generated runs are stored under `.fabtrace/runs/` and are ignored by Git. Each
run contains:

- `effective_config.json`: resolved, reproducible source and synthetic inputs
- `run_manifest.json`: execution metadata and source-file fingerprints
- `ground_truth.jsonl`: private structured simulation events
- `provider_updates.jsonl`: natural-language messages for future extraction

Ground truth is evaluation data and must not be consumed by the future message
extraction pipeline. Stochastic draws, thresholds, and sampled outcomes are
recorded only in private ground truth; provider updates contain operational
facts without simulator internals.

## Raw provider-update ingestion

Ingest a bounded provider-message file into the default local database:

```bash
uv run fabtrace ingest provider-updates \
  --input .fabtrace/runs/stochastic-disruption-seed-42/provider_updates.jsonl
```

Generated state is stored in `.fabtrace/state/fabtrace.db` and ignored by Git.
The ingestion stage provides at-least-once delivery semantics with idempotent
accepted-message writes. Replaying the same file reports duplicates without
rewriting stored rows. A reused `message_id` with different payload bytes is an
identity conflict and is quarantined.

The database keeps storage responsibilities logically separate:

- `ingestion_batches`: bounded invocation metadata and outcome counts
- `raw_provider_messages`: validated, immutable provider updates and exact bytes
- `quarantined_provider_messages`: immutable rejected bytes and diagnostics

Inspect the latest batches and retained-message count:

```bash
uv run python -m sqlite3 .fabtrace/state/fabtrace.db \
  "SELECT status, total_lines, accepted_count, duplicate_count, quarantined_count FROM ingestion_batches ORDER BY rowid;"

uv run python -m sqlite3 .fabtrace/state/fabtrace.db \
  "SELECT COUNT(*) AS retained_messages FROM raw_provider_messages;"
```

The ingestion path accepts only the public provider-message schema. Private
`ground_truth.jsonl` records fail validation and are quarantined rather than
becoming extraction input.

## Structured claim extraction

Run the current deterministic extractor against one explicitly bounded
scenario:

```bash
uv run fabtrace extract provider-updates \
  --database .fabtrace/state/fabtrace.db \
  --scenario-id stochastic-disruption
```

`rules-v2` reads only accepted raw message subjects and bodies. It emits
append-only typed claims for status, progress, quantities, unit, forecast date,
delay, incident reason, and milestone date. Every claim links to its raw message
and extraction run and retains the exact source span, normalized value,
extractor version, confidence, and validation status. It recognizes both legacy
`PO-0001` references and complete plan-generated IDs such as
`ORDER-0003-PO-001` and `ORDER-0003-WO-001`.

Exact supported patterns receive confidence `1.0`. Unsupported text abstains;
ambiguous or conflicting order references require review. Replaying `rules-v2`
skips previously completed messages instead of duplicating claims.
Pass `--extractor-version rules-v1` only when intentionally reproducing the
legacy baseline.

Evaluate completed claims through the separate private-truth path:

```bash
uv run fabtrace evaluate extraction \
  --database .fabtrace/state/fabtrace.db \
  --ground-truth .fabtrace/runs/stochastic-disruption-seed-42/ground_truth.jsonl \
  --scenario-id stochastic-disruption
```

The evaluator reports exact field-level precision and recall, missing and
incorrect values, unexpected claims, and abstentions. Ground truth is opened
only by this evaluation command after extraction; it is never an extractor
input.

## Order-state reconciliation

Reconcile eligible `rules-v2` claims for one bounded scenario:

```bash
uv run fabtrace reconcile order-state \
  --database .fabtrace/state/fabtrace.db \
  --scenario-id stochastic-disruption
```

`order-state-v2` processes messages in received-time order and retains provider
sent time as each fact's effective time. It creates an immutable decision for
every message and an append-only snapshot whenever state changes. Older updates
are marked stale; illegal lifecycle transitions, same-time disagreements,
invalid regressions, and unresolved extractions become conflicts without
changing state.

The `current_order_state` view exposes the latest snapshot at the grain of
scenario, purchase order, and reconciliation version. Every field carries the
message ID and effective time from which it originated.

Inspect current state:

```bash
uv run python -m sqlite3 .fabtrace/state/fabtrace.db \
  "SELECT purchase_order_id, order_status, progress_pct, quantity_completed, quantity_ordered, quantity_unit, forecast_delivery_date, delay_days, incident_reason FROM current_order_state WHERE reconciliation_version = 'order-state-v2';"
```

The dashboard prefers `order-state-v2` for each provider order and falls back
to retained `order-state-v1` state when no v2 projection exists. Purchase and
work orders are shown separately and roll up through registered customer plans.

This is source-reported operational state, not a predictive forecast or risk
score. Predictive ETA, risk, cost, quality analytics, and human conflict
resolution remain separate future contracts.

## Operational analytics

Compute a reproducible metric snapshot from reconciled state using an explicit,
timezone-aware cutoff:

```bash
uv run fabtrace analyze orders \
  --database .fabtrace/state/fabtrace.db \
  --scenario-id stochastic-disruption \
  --as-of 2026-01-16T00:00:00Z
```

`operational-metrics-v1` emits one append-only row per scenario, purchase order,
metric version, reconciliation version, and normalized UTC `as_of_time`. A replay
at the same instant is idempotent. Only snapshots and decisions received by the
cutoff are eligible, preventing future updates from leaking into historical
metrics.

The metric grain and definitions are:

- `order_status` and `progress_pct`: latest reconciled values at the cutoff.
- `initial_forecast_date` and `current_forecast_date`: first and latest
  non-NULL provider forecasts observed by the cutoff.
- `forecast_drift_days`: signed calendar-day difference from initial to current
  forecast; positive means later delivery and negative means earlier delivery.
- `forecast_revision_count`: number of changes between distinct non-NULL
  forecast values.
- `days_to_forecast`: signed calendar days from the UTC cutoff date to the
  current provider forecast.
- `reported_delay_days`: latest provider-reported delay. It remains `NULL` when
  no delay was reported; `has_reported_delay` distinguishes that case.
- `progress_velocity_pct_per_day`: percentage-point change per elapsed day using
  the latest two distinct progress observations. It remains `NULL` until two
  usable observations exist.
- `latest_update_age_days`: elapsed days from the latest received update to the
  cutoff.
- `communication_lag_days`: elapsed days between the latest update's provider
  sent time and platform received time.
- `applied_message_count`, `stale_message_count`, and
  `conflict_message_count`: reconciliation outcomes received by the cutoff.

Inspect the latest computed metric snapshot for every order:

```bash
uv run python -m sqlite3 .fabtrace/state/fabtrace.db \
  "SELECT purchase_order_id, order_status, progress_pct, current_forecast_date, forecast_drift_days, reported_delay_days, progress_velocity_pct_per_day FROM current_order_metrics WHERE metric_version = 'operational-metrics-v1';"
```

These are transparent operational indicators, not a predicted ETA or composite
risk score. Prediction, scoring, and automated interventions remain separate
future contracts.

## Dashboard, Product Designer, and Planner

Start the local development server against an existing pipeline database:

```bash
uv run fabtrace serve \
  --database .fabtrace/state/fabtrace.db \
  --data-dir Data \
  --host 127.0.0.1 \
  --port 8000
```

Then open:

- Dashboard: <http://127.0.0.1:8000/>
- Provider view: <http://127.0.0.1:8000/providers>
- Product catalog: <http://127.0.0.1:8000/products>
- New product designer: <http://127.0.0.1:8000/products/new>
- Generated plans: <http://127.0.0.1:8000/plans>
- Interactive API documentation: <http://127.0.0.1:8000/docs>
- Health check: <http://127.0.0.1:8000/health>

The versioned JSON read API exposes:

- `GET /api/v1/customer-orders?scenario_id=...`: registered customer plans
  rolled up from their reconciled provider orders.
- `GET /api/v1/customer-orders/{order-id}?scenario_id=...`: plan dependency,
  purchase/work-order execution, and projection provenance.
- `GET /api/v1/orders?scenario_id=...`: current order state joined to the
  latest computed metric snapshot.
- `GET /api/v1/orders/{scenario_id}/{order_id}`: current values, immutable
  state history, metric history, reconciliation decisions, and provenance.
- `GET /api/v1/providers?scenario_id=...`: orders grouped by provider identity
  without a provider ranking or composite score.

The service opens SQLite with `mode=ro` and `PRAGMA query_only`; it does not
initialize, migrate, or modify the database. API and dashboard values retain
separate state-observation and metric-as-of timestamps. Missing values remain
`null` in JSON and are labeled as missing or not computed in HTML.

Jinja renders complete pages on the server. HTMX progressively enhances the
order filter, but the normal HTML form remains functional if JavaScript or the
HTMX CDN is unavailable. This local server has no authentication or production
deployment contract; keep it bound to `127.0.0.1` unless that boundary is
revisited explicitly.

### Visual product configuration

Product Designer v1 uses a pinned Cytoscape.js graph canvas. Add material,
process, reusable-capability, and final-output nodes; connect a prerequisite to
the node that depends on it; and select nodes or edges to edit their properties.
The editor continuously previews the generated flat recipe JSON and Mermaid
source.

Use the editor in this order:

1. Set a lowercase product ID and display name.
2. Add nodes and configure each node's output unit, optional ETO reference,
   process duration, and provider candidates.
3. Choose **Connect nodes**, click the prerequisite first, then click the
   dependent node. Configure quantity and unit on the resulting edge.
4. Add exactly one final-output node and connect every used branch to it.
5. Save the mutable draft, validate it, and publish an immutable version.

For material and process nodes, entering or changing an ETO reference
automatically reads matching organization providers from the immutable
`provision.csv` relationships. The editor fills provider IDs in deterministic
ID order and shows source names for context. This is discovery, not a provider
ranking: country rows are excluded, unknown market share remains unknown, and
no reliability or quality is inferred. The comma-separated field remains
editable, and its first ID is still Planner v1's selected candidate. Use
**Refresh providers from ETO** to restore the sourced list after manual edits.

Material lead time is deliberately not a Product Designer input. Planner v1
temporarily retains a one-day synthetic compatibility value in recipe JSON;
this is not a sourced estimate or a user decision. A later planner version will
derive order-specific timing from the selected provider, ordered quantity,
capacity, processing, quality checks, and transport assumptions.

Server-side publication requires exactly one terminal output, valid references,
positive edge quantities, matching units, providers for materials and
processes, positive process durations, no cycles, and no nodes disconnected
from the output. Browser checks are only feedback; the server is authoritative.

Mutable work is stored under `.fabtrace/drafts/products/` and ignored by Git.
Published versions are created under
`.fabtrace/config/products/<product-id>/v<N>.json` and are never overwritten.
Editing a published version creates a new draft instead of mutating history.

The operational SQLite connection remains read-only. Product write endpoints
are limited to the two product directories, accept only validated safe IDs,
require an editor-specific request marker, and reject non-local clients. The
service still has no authentication or multi-user concurrency contract.

The product-configuration API is available under `/api/v1/product-configs`.
It supports listing configurations, saving and validating drafts, publishing
versions, reading immutable versions, and starting a new draft from a published
version.

The read-only source-reference endpoint
`GET /api/v1/source/capabilities/{eto-id}/providers` returns organization
providers recorded for an ETO input or stage, including source names, country
codes, provision years, aliases, and nullable reported shares.

### Deterministic product planning

After publishing a valid product, choose **Plan order** in the product catalog.
Provide a unique order ID, positive output quantity, and timezone-aware ISO 8601
request time. Planner v1 recursively expands dependency quantities, chooses the
first provider candidate, generates one purchase order per material and one
work order per process, and forward-schedules the graph. Independent branches
run in parallel; a node starts when its latest prerequisite finishes. The final
output finish is the expected delivery and the longest deterministic branch is
shown as its critical path.

The same operation is available from the CLI:

```bash
uv run fabtrace plan order \
  --product-id logic-chip \
  --product-version 1 \
  --order-id ORDER-0001 \
  --quantity 10 \
  --requested-at 2026-01-05T00:00:00Z
```

Plans are immutable JSON artifacts at
`.fabtrace/plans/<order-id>/plan.json`. Reusing an order ID fails instead of
overwriting its history. Every artifact records its published recipe version
and SHA-256 fingerprint, expanded node demand, UTC schedule, provider-order
dependencies, critical path, and modeling assumptions.

Planner v1 is a transparent synthetic baseline, not a real-world forecast. It
does not model inventory, provider capacity or queues, transport, business
calendars, batch sizes, yield, rework, costs, or probabilistic delivery. Product
versions and source plan artifacts remain stored separately under `.fabtrace/`.

The planning API is available at `GET/POST /api/v1/plans` and
`GET /api/v1/plans/{order-id}`. Browser writes remain local-development-only
and require the same mutation marker as Product Designer.

### Customer-order registration and rollup

Register an immutable plan in the operational database before expecting it on
the main Orders dashboard:

```bash
uv run fabtrace register plan \
  --plan .fabtrace/plans/ORDER-0001/plan.json \
  --database .fabtrace/state/fabtrace.db
```

The same order ID and exact SHA-256 is an idempotent replay. Reusing an order ID
with different plan bytes is rejected. Registration validates provider-order
identity, references, duplicate dependencies, cycles, timestamps, and planned
node links, then stores append-only plan, provider-order, and dependency rows.
It does not modify the source JSON.

`customer-order-v1` joins those planned provider orders to preferred reconciled
execution (`order-state-v2`, with v1 fallback). The customer status is
`DELIVERED` only when all terminal provider orders are delivered, `BLOCKED`
when a critical-path provider order reports blocked, `IN_PROGRESS` when any
provider state exists, and otherwise `PLANNED`. Progress is shown as completed
provider orders over total provider orders; percentages across unlike units are
never averaged. The current delivery estimate is the latest date across
reported terminal-order forecasts, falling back visibly to the planned date.

The main dashboard lists customer orders. Provider-order state remains
available through the existing `/api/v1/orders` endpoints and evidence pages.

### Checkpointed plan pipeline

Run the complete local workflow with one command:

```bash
uv run fabtrace pipeline execute-plan \
  --plan .fabtrace/plans/ORDER-0001/plan.json \
  --scenario .fabtrace/config/plan-scenarios/default.json \
  --data-dir Data \
  --database .fabtrace/state/fabtrace.db \
  --output-dir .fabtrace/runs
```

`execute-plan-v1` invokes the existing registration, plan simulation,
ingestion, rules-v2 extraction, order-state-v2 reconciliation, and
operational-metrics-v1 functions. It does not duplicate their business logic.
The analytics cutoff is the latest retained `received_at` timestamp for the
bounded scenario, and that exact timestamp is recorded with the metric run.

Schema v6 retains an immutable pipeline identity and append-only run, stage
attempt, and stage outcome events. The identity fingerprints the plan,
scenario, five ETO source files, component versions, database target, and run
directory. Repeating the exact command reuses completed stage checkpoints; a
failed or interrupted stage receives a new attempt while completed upstream
stages remain untouched. Existing simulation output is reused only after its
manifest, plan, effective configuration, ETO fingerprints, required files, and
record counts pass validation. FabTrace never overwrites a run directory.

The customer-order detail page and API include pipeline status, attempts,
timings, and errors. This is deliberately synchronous and local: no scheduler,
worker service, broker, or orchestration framework is required at this stage.

If the package has not been installed, the validation command can also be run
directly from the repository:

```bash
PYTHONPATH=src python3 -m fabtrace data validate --data-dir Data
```

## Source tables

- `Data/stages.csv`
- `Data/inputs.csv`
- `Data/sequence.csv`
- `Data/providers.csv`
- `Data/provision.csv`

Treat these files as immutable source snapshots. Cleaning and synthetic
enrichment belong in derived models or scenario configuration.
