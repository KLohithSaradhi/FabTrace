---
name: fabtrace-event-pipeline
description: Design, implement, review, or debug FabTrace event envelopes, ingestion, append-only raw storage, delivery semantics, replay, deduplication, schema evolution, and dead-letter handling. Do not use to generate provider behavior, extract natural language, define business metrics, or build UI components.
---

# FabTrace Event Pipeline

Move provider and order events into durable raw storage without losing their
original content, temporal meaning, provenance, or replayability.

## Confirm infrastructure choices

Read [AGENTS.md](../../../AGENTS.md). Obtain approval before selecting a broker,
object store, database, serialization format, schema registry, orchestration
tool, or delivery guarantee. Do not add distributed infrastructure solely to
make the architecture appear realistic.

Define the event contract before coupling producers and consumers. Keep
transport metadata separate from the domain payload where useful.

## Preserve event semantics

An event envelope should cover the approved subset of:

- Stable event ID and event type
- Schema and producer version
- Scenario, provider, order, and correlation IDs
- Event time, sent time, ingestion time, and processing time
- Source channel and provenance
- Payload and units
- Synthetic-data marker
- Trace or causation identifiers

Distinguish event time from processing time. Do not overwrite raw messages after
ingestion. Corrections and retractions are new events linked to earlier events.

Treat at-least-once delivery, duplicates, late arrival, and out-of-order arrival
as normal conditions when supported by the approved design. Make consumers
idempotent using event identity rather than relying on timing.

## Separate storage responsibilities

Keep these concerns distinct even when one technology initially serves several:

- Producer publication
- Transport and retry
- Immutable raw retention
- Validation and quarantine
- Normalized downstream consumption
- Replay and backfill control

Never expose private simulator ground truth to the message-extraction consumer.
Ground truth may use a separately authorized evaluation path.

Schema evolution must be explicit and compatible with retained events. Unknown
or malformed events should remain inspectable in quarantine or a dead-letter
path with their failure reason; do not silently discard them.

## Operate and verify

Make replay bounded by explicit event ranges, scenarios, or partitions so it
cannot duplicate an entire environment accidentally. Record checkpoints and
backfill metadata separately from source events.

Test applicable behavior:

- Duplicate delivery does not duplicate normalized facts.
- Late and out-of-order events retain correct event time.
- Invalid schemas are quarantined with useful diagnostics.
- Raw payloads survive round trips unchanged.
- Replay yields the same logical state.
- Consumer failure and retry do not lose acknowledged events.
- Schema-version behavior is covered by compatibility tests.

Report delivery semantics honestly. Avoid claims such as "exactly once" unless
the complete end-to-end behavior proves them.
