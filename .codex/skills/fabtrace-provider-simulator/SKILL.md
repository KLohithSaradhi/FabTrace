---
name: fabtrace-provider-simulator
description: Build, review, or debug FabTrace provider profiles, purchase-order lifecycles, simulation scenarios, structured ground-truth events, and natural-language provider updates. Use for provider-agent and provider-log work; do not use for the later ETL platform, analytics models, dashboard, or deployment unless the task also changes the simulator contract.
---

# FabTrace Provider Simulator

Build a deterministic, stateful simulation of semiconductor providers that
produces operational truth and realistic provider communications for downstream
data-engineering work.

## Before changing code

Read [AGENTS.md](../../../AGENTS.md) and follow its collaboration contract. In
particular, do not treat a proposed technology, schema, state machine, event
type, or directory layout as approved. Identify any material decision needed
for the task, recommend a default with tradeoffs, and obtain the user's explicit
approval before implementing it.

Inspect the current tree and working state before editing. Preserve user changes
and keep `Data/*.csv` immutable.

## Use the ETO data correctly

Load CSV files with a CSV-aware parser because descriptions contain commas and
newlines. Join records by IDs rather than names:

- `providers.provider_id` to `provision.provider_id`
- `inputs.input_id` or `stages.stage_id` to `provision.provided_id`
- `inputs.input_id` to the node IDs in `sequence.csv`

Simulated provider agents should normally come from organization records.
Country records describe geography or market-level provision unless another
interpretation has been explicitly approved.

Treat ETO identities, capabilities, relationships, market shares, years, and
citations as sourced reference facts. Treat capacities, lead times, queues,
prices, reliability, yields, defects, orders, shipments, and incidents as
synthetic. Label synthetic values and make their generation reproducible.

Do not use market share as evidence of reliability, quality, price, or risk. If
an approved scenario uses market share to influence capacity or provider
selection, record that as a simulation assumption.

## Preserve the simulation boundary

Generate structured operational truth before generating prose. A provider
message is a lossy communication derived from a simulation event, never the
authoritative state itself.

Maintain a stable relationship among:

1. Scenario and seed
2. Provider profile and purchase order
3. Structured ground-truth event
4. Raw provider message derived from that event

Keep ground truth logically separate from the consumer-facing message stream.
The future extraction pipeline must not read private truth fields. This
separation is required so extraction and forecast accuracy can be evaluated.

## Model provider behavior causally

Use explicit state and legal transitions. Do not emit unrelated random values
for progress, quality, risk, cost, and delivery forecasts.

An incident must first affect operational state. Derived dates, quantities, or
costs then change, and the provider may communicate the change. Examples:

- An equipment failure reduces capacity or pauses work, changing the forecast.
- A quality failure reduces accepted quantity or creates rework.
- A capacity constraint increases queue time before production begins.
- A shipment event can occur only after its approved prerequisites.

Generate both routine periodic updates and event-driven messages when the
approved scenario calls for them. Controlled ambiguity, duplicate delivery,
late arrival, and out-of-order arrival are useful test scenarios, but underlying
truth must remain internally consistent.

Use a virtual clock and deterministic random streams when supported by the
approved design. Derive randomness from stable scenario, provider, and order
identifiers rather than process-dependent iteration order.

## Keep contracts explicit

For every approved event or message model, define and validate the fields that
matter to downstream processing. Consider:

- Stable identifiers and correlation IDs
- Scenario and generator versions
- Provider, order, and capability IDs
- Event time, sent time, and received/ingested time
- Status and event type
- Quantities and units
- Currency and price semantics
- Forecast dates and delay duration
- Message channel and body
- Synthetic-data and provenance markers

Represent corrections as new events instead of rewriting historical events.
Design publishers behind a narrow boundary so storage or transport can change
without coupling it to provider behavior, but do not introduce a broker or
database before approval.

## Implement in thin vertical slices

Prefer an end-to-end slice that loads a small approved provider set, executes
one purchase order, emits linked truth and message records, and verifies the
timeline. Expand provider count, incidents, message diversity, persistence, and
streaming only after that slice is correct.

Keep these responsibilities separable even if the initial implementation is
small:

- Source-data loading and validation
- Synthetic profile/scenario configuration
- Provider and order state transitions
- Event creation
- Natural-language rendering
- Publication or output

Do not add an LLM merely to vary wording. Start with deterministic rendering if
that is the approved design. If LLM paraphrasing is later approved, preserve the
input facts, model/version metadata, and original deterministic representation,
and test that dates, quantities, statuses, and negation do not drift.

## Verify behavior

Add tests proportionate to the changed contract. At minimum, cover applicable
invariants such as:

- A fixed scenario version and seed reproduce the same logical events.
- Every provider and capability reference exists in the source data.
- Invalid state transitions are rejected.
- Event chronology is consistent with the simulation clock.
- Quantities remain nonnegative and progress obeys the approved semantics.
- Shipping and delivery cannot precede required states.
- Every provider message links to the event that generated it.
- Message facts agree with ground truth before deliberate ambiguity is applied.
- Malformed configuration fails with a useful error.

Run the relevant tests and report the exact commands and results. If a check
cannot run, explain why rather than claiming completion.

## Handoff

Summarize:

- What provider behavior now exists
- Which choices were approved and implemented
- Which data is sourced versus synthetic
- The observable simulation output
- Tests and validation performed
- Remaining material decisions for the next slice
