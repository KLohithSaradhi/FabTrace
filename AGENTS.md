# FabTrace Agent Guide

## Project mission

FabTrace is an event-driven semiconductor supply-chain digital twin and control
tower. It is a learning project focused on data engineering: ingestion, ETL,
event processing, orchestration, data quality, analytical modeling, forecasting,
and observability.

The intended user experience is:

1. A user configures a product's dependency graph and candidate providers.
2. The user places an order for a quantity of that product.
3. FabTrace expands the order into its material and process requirements.
4. FabTrace selects or allocates providers and creates purchase/work orders.
5. Simulated providers execute those orders and publish periodic and
   event-driven natural-language updates.
6. Data pipelines retain the raw updates and extract structured facts.
7. The system derives progress, delivery forecasts, risk, quality, and cost.
8. A dashboard shows the current state and how it changed over time.

The first active milestone is the provider simulator. Do not jump ahead to the
dashboard or a production-scale platform until the simulation contracts are
agreed and tested.

## Collaboration contract

The user wants every material decision confirmed before it is implemented.

`log.txt` is the append-only project decision log. Every approved, rejected, or
superseded material decision must be recorded there. When implementing an
approved decision, update `log.txt` in the same change.

`tradeoffs.txt` is the append-only design rationale log. For every major design
decision, record the realistic alternatives considered, why the selected option
fits the current milestone, its disadvantages and risks, and a concrete trigger
for revisiting it. Update `tradeoffs.txt` in the same change as the decision.
`log.txt` records what and status; `tradeoffs.txt` records why and cost.

- Before adding a dependency, service, database, broker, framework, schema,
  state transition, scoring formula, or major directory boundary, explain the
  recommended choice and its tradeoffs and obtain explicit approval.
- When multiple reasonable interpretations exist, ask instead of silently
  choosing one.
- Distinguish recommendations from decisions that the user has approved.
- Do not treat a previously proposed option as approved merely because it was
  presented in a plan.
- Once a decision is approved, routine mechanical implementation consistent
  with it does not require repeated confirmation. Surface any deviation before
  making it.
- Keep questions grouped and concise. Offer a recommended default for each.
- Lead status reports with outcomes, then verification evidence and remaining
  decisions.
- Give each logged decision a stable ID, date, status, decision, rationale, and
  consequences. Record unresolved proposals separately as pending items.
- Never erase or silently rewrite decision history. Append a new entry that
  supersedes or corrects the earlier decision and reference its ID.
- Give each major tradeoff a stable ID and link it to the relevant decision IDs.
  Do not manufacture weak alternatives after implementation merely to justify a
  choice; record genuine options and uncertainty.
- Never erase or rewrite tradeoff history. Append a new entry when evidence,
  scale, requirements, or constraints change the preferred design.

## Current repository state

The repository currently contains the following copied source tables under
`Data/`:

- `Data/stages.csv`
- `Data/inputs.csv`
- `Data/sequence.csv`
- `Data/providers.csv`
- `Data/provision.csv`

They were copied byte-for-byte from the sibling `eto-chip-explorer` repository.
Treat them as immutable source data. Do not edit them to accommodate application
logic. Put cleaning, normalization, enrichment, and synthetic attributes in
derived data or code.

The capitalized directory name `Data/` is intentional. Do not rename it without
approval.

## Source-data model

The ETO files are research/reference data about the advanced semiconductor
supply chain. They are not operational order records.

### `stages.csv`

Contains three high-level stages:

- `S1`: Design
- `S2`: Fabrication
- `S3`: Assembly, testing, and packaging (ATP)

### `inputs.csv`

Contains 126 graph nodes identified by `input_id`:

- 90 `tool_resource` nodes
- 17 `material_resource` nodes
- 11 `process` nodes
- 7 `design_resource` nodes
- 1 `ultimate_output` node (`N99`, Finished logic chip)

The word "input" is broad here: a row may represent a process, tool, material,
design resource, or final product. Market-size fields are presentation strings,
not normalized numeric values. Years vary across records.

### `sequence.csv`

Contains two mutually exclusive relationship types:

- `goes_into`: a supply-chain/process dependency
- `is_type_of`: a taxonomy or specialization relationship

These edges do not define quantities, durations, yields, substitutions,
mandatory versus optional inputs, or execution semantics. Do not present this
table as a production-ready bill of materials.

### `providers.csv`

Contains country and organization records. Some logical providers have multiple
rows because they have multiple aliases; use `provider_id` as the identity key.

Provider agents should normally represent `organization` records. Country rows
are geographic or market-level reference data, not autonomous suppliers, unless
the user explicitly approves a different model.

### `provision.csv`

Maps providers to supplied inputs or stages. Join it using `provider_id` and
`provided_id`; duplicated names are display labels. A blank `share_provided`
does not mean zero supply. Percentage totals may not equal exactly 100.

Market share may inform a clearly documented synthetic capacity or selection
assumption. It must not be treated as evidence of reliability, quality, price,
or lead time.

## Facts versus simulation

Always preserve the boundary between sourced facts and synthetic operational
data.

ETO-derived facts include:

- Node, stage, and provider identities
- Graph and taxonomy relationships
- Provider capability relationships
- Country associations
- Reported market-share information and citations

Synthetic facts will include, unless a future source is added:

- Capacity and utilization
- Inventory and queue length
- BOM quantities and production yields
- Processing and transport durations
- Prices and commercial terms
- Reliability and defect rates
- Purchase orders, shipments, incidents, and status histories

Synthetic fields must be visibly labeled as synthetic in schemas,
documentation, and user-facing output. They should be reproducible from a
scenario version and random seed.

Do not infer that a large provider is reliable, that a country is risky, or that
market share implies quality. Avoid embedding unsupported real-world claims in
generated profiles.

## Provider-simulation invariants

The provider simulator must be stateful. Generate structured operational truth
first and render unstructured provider messages from that truth. Never create
independent random prose and later pretend it is authoritative state.

For every emitted provider update, retain:

1. The private structured simulation event (ground truth).
2. The raw provider-facing message.
3. A stable link between the two for later evaluation.

The future extraction pipeline must consume the raw message stream, not the
private ground-truth stream. Ground truth exists to evaluate extraction and
forecast accuracy.

Provider updates should eventually include both:

- Periodic routine updates
- Event-driven incident or milestone updates

Simulation outcomes must be causal. For example, an equipment failure changes
capacity or duration, which changes the forecast, which causes a message. Risk,
progress, cost, and quality must not be unrelated random numbers.

Prefer a virtual clock and deterministic seeds so scenarios can run quickly and
tests are repeatable. The exact simulation library and runtime architecture
still require user approval.

## Order-planning rules

Do not create an order for every provider listed against a requirement.
Providers in ETO are candidates. Planning must eventually decide whether to
select one provider, split an allocation, or use an existing capability.

Distinguish among:

- Consumable materials
- Outsourced process services
- Design resources or licenses
- Reusable capital equipment
- Alternative/specialized variants

For example, a lithography machine should not automatically become a purchase
order line for every chip-production order.

Delivery estimation must respect dependency order and parallel branches. The
final forecast is driven by the critical path, capacity queues, processing
times, transport, yield, and rework—not a simple sum across all providers.

## Event and data-engineering principles

When event schemas are approved, design them for:

- Stable event and entity identifiers
- Event time and ingestion/processing time
- Schema and generator versions
- Idempotency and deduplication
- Late and out-of-order arrival
- Replayability
- Units, currencies, and time zones
- Provenance and extraction confidence
- Quarantine/dead-letter handling

Raw events must be append-only. Corrections should be represented as new events,
not destructive edits to history.

Keep raw, normalized, and analytical data conceptually separate. Derived
metrics must remain traceable to normalized facts, raw messages, and ultimately
the simulation event used for evaluation.

Do not introduce Kafka/Redpanda, a database, orchestration, an LLM, Spark,
Kubernetes, or cloud infrastructure until its role and timing are approved.
Favor the smallest component that teaches the intended engineering concept.

## Quality expectations

Any implemented milestone should include proportionate automated tests. For the
provider simulator, tests should eventually cover:

- Deterministic output for a fixed seed and scenario version
- Valid references to source provider and capability IDs
- Legal state transitions
- Causal and chronological consistency
- Quantity, date, price, and unit invariants
- No shipping before required production and quality steps
- Stable links between truth events and raw messages
- Schema validation and malformed-input behavior
- Duplicate, delayed, and out-of-order event scenarios

Do not claim completion without running the relevant tests. Report the exact
checks run and any checks that could not be run.

## Working practices

- Inspect existing files and repository status before editing.
- Preserve user changes and avoid unrelated rewrites.
- Use IDs rather than display names for joins.
- Keep source ingestion separate from simulation behavior.
- Make scenario configuration explicit and versionable; avoid unexplained magic
  constants in code.
- Use UTC internally for event timestamps unless the user approves another
  convention. Preserve source-local context separately when needed.
- Never commit secrets, generated credentials, or private endpoints.
- Do not commit large generated event runs unless they are approved fixtures.
- Update project documentation when an approved contract or command changes.
- Prefer clear domain names over generic names such as `data`, `manager`, or
  `utils` inside application code.

## Stage completion handoff

At the end of every implementation stage, provide a self-contained handoff that
includes:

1. A concise summary of what was implemented in that stage.
2. The important behavior, data contracts, and boundaries now in effect.
3. Copy-paste executable commands, starting from the FabTrace repository, that
   let the user install/synchronize dependencies, exercise the stage, and run
   its automated tests.
4. The expected success output or observable artifacts for each command.
5. The exact verification commands the agent ran and their results.
6. Decisions and tradeoff entries added during the stage.
7. Features intentionally left out and the next decision or milestone.

Keep "commands to run" separate from "commands already verified" so the user
can distinguish instructions from evidence. Account for safety behavior such as
run-directory overwrite protection; do not give a command that will immediately
fail against artifacts generated during implementation without explaining how
to choose a fresh output path or intentionally archive the old run.

## Definition of done for the first provider milestone

The first provider milestone is complete only after the user approves its
technology and domain contracts and the implementation can:

1. Load selected organization providers and capabilities from `Data/`.
2. Apply explicitly synthetic, reproducible provider profiles.
3. Accept a small configured set of purchase orders.
4. Advance each order through an approved lifecycle.
5. Generate routine and exceptional structured events.
6. Render linked natural-language provider updates.
7. Preserve ground truth separately from the consumer-facing stream.
8. Reproduce the same run from the same scenario version and seed.
9. Reject invalid configurations with useful errors.
10. Pass the agreed automated tests.

The output format, package structure, language version, simulation framework,
initial providers, event types, state machine, and execution interface remain
material decisions until explicitly approved.
