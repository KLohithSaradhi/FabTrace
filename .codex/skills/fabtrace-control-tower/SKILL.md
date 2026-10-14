---
name: fabtrace-control-tower
description: Design, implement, review, or debug FabTrace user workflows, APIs, and dashboard views for orders, dependency graphs, providers, progress, cost, quality, risk, forecasts, and message traceability. Do not use to redefine simulation, ingestion, extraction, or analytical contracts.
---

# FabTrace Control Tower

Expose FabTrace planning and operational intelligence through an understandable,
traceable order-management and monitoring experience.

## Respect component boundaries

Read [AGENTS.md](../../../AGENTS.md). Confirm the UI framework, API boundary,
authentication scope, deployment target, and major interaction design before
implementing them.

Consume approved contracts from planning, events, extraction, and analytics.
Do not calculate authoritative business metrics independently in frontend code
or silently redefine upstream semantics to make a view convenient.

## Support the core workflows

Design the approved subset of:

- Configure or select a versioned product recipe
- Review requirements and candidate providers
- Place an order and inspect generated purchase/work orders
- Follow dependency and critical-path status
- View progress, cost, quality, risk, and delivery forecasts
- Compare current estimates with original plans and prior forecasts
- Inspect provider updates and the facts extracted from them
- Explain metric provenance, confidence, and synthetic assumptions
- Run approved what-if scenarios without altering recorded actuals

Make sourced ETO data, synthetic scenario data, extracted claims, and derived
metrics visually distinguishable where confusion is possible.

## Present uncertainty honestly

Show forecast quantiles or ranges when supplied instead of collapsing them into
an unexplained date. Make stale data, low extraction confidence, missing
providers, conflicting updates, and partial progress visible.

Graph views should distinguish process dependencies from taxonomy
relationships. They should not imply that every provider is selected or every
tool is purchased for the current order.

Provide drill-down from summary metric to order/work item, normalized fact,
provider message, and provenance where contracts allow it. Avoid color-only
status encoding and unexplained red/amber/green scores.

## API and state behavior

Keep commands, queries, and scenario simulations distinguishable. Validate
inputs server-side at the authoritative boundary. Use stable IDs in routes and
payloads; names are display labels.

Order placement and other mutations must handle retries without accidental
duplicate orders. Loading, empty, partial, stale, and error states are first-
class behavior, not afterthoughts.

## Verify the experience

Test applicable workflows and contracts:

- Order submission success, validation failure, and retry
- Dependency graph semantics and selected-provider distinction
- Metric and forecast rendering with units and as-of times
- Low-confidence, conflicting, late, and missing updates
- Traceability from summaries to underlying evidence
- Accessibility of controls, status, tables, and graphs
- API contract compatibility and authorization boundaries

Report which workflows are complete, the upstream contracts assumed, UI/API
tests run, accessibility checks performed, and remaining product decisions.
