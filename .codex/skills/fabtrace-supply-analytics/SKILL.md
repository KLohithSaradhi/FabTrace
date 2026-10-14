---
name: fabtrace-supply-analytics
description: Define, implement, validate, or debug FabTrace analytical models for order progress, supplier performance, cost, quality, risk, critical paths, and delivery forecasts. Do not use for raw ingestion, provider-message extraction, simulation behavior, or dashboard presentation.
---

# FabTrace Supply Analytics

Turn normalized operational facts into explainable, historically reproducible
metrics and forecasts without hiding uncertainty or source quality.

## Confirm metric contracts

Read [AGENTS.md](../../../AGENTS.md). Before implementing a metric, obtain
approval for its business definition, grain, time semantics, weighting,
thresholds, handling of missing data, and refresh expectations. Obtain separate
approval for forecasting or scoring algorithms and their evaluation criteria.

Do not invent a composite "AI risk score" without decomposable, approved
components. Do not substitute ETO market share for supplier reliability,
quality, price, or geopolitical risk.

## Build traceable analytical models

Keep raw, normalized, and analytical layers conceptually distinct. Every metric
should identify:

- Entity and grain
- As-of time or validity interval
- Inputs and transformations
- Units and aggregation rules
- Source and extraction confidence where relevant
- Model or definition version

Retain historical snapshots or valid-time state so users can reconstruct what
was known when a forecast or decision was made. Avoid future-data leakage into
historical metrics and forecast training/evaluation.

Progress should respect approved task weights, quantities, dependencies, and
rework semantics. Cost should distinguish planned, committed, actual, and
forecast-at-completion values. Quality should distinguish produced, accepted,
rejected, and reworked quantities.

Delivery forecasts must respect dependency order, parallel paths, queues,
processing, transit, yield, and rework. Surface the critical path and assumption
changes. If probabilistic forecasting is approved, report distributions or
quantiles such as P50/P90 and calibration rather than false precision.

Risk should remain interpretable, for example schedule probability and impact,
supplier concentration, capacity exposure, quality trend, or data-confidence
risk. Preserve component values behind any approved summary score.

## Test analytical correctness

Cover applicable checks:

- Grain and key uniqueness
- Referential integrity to normalized entities
- Unit and currency consistency
- Reconciliation between components and totals
- No impossible percentages or negative quantities
- No future information in historical snapshots
- Critical-path behavior on parallel and delayed branches
- Forecast backtesting, calibration, and baseline comparison
- Stable results for the same versioned inputs

Use small hand-computable scenarios for metric tests. Report definitions,
assumptions, freshness, data-quality limitations, and forecast performance.
Provide dashboard-ready outputs without embedding presentation logic in the
analytical layer.
