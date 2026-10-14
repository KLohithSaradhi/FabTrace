---
name: fabtrace-order-planning
description: Design, implement, review, or debug FabTrace product recipes, dependency expansion, provider selection, allocations, purchase orders, work orders, and planned schedules. Do not use for provider execution simulation, raw event transport, message extraction, analytics, or dashboard rendering.
---

# FabTrace Order Planning

Convert a requested product and quantity into an explainable, executable supply
plan without pretending the ETO graph is already a quantitative BOM.

## Establish the approved contract

Read [AGENTS.md](../../../AGENTS.md). Before implementation, obtain approval for
the recipe semantics, planning horizon, selection policy, allocation rules,
quantity units, and scheduling assumptions relevant to the task.

Use ETO IDs for reference entities, but store synthetic operational attributes
separately. Every plan should identify its recipe version, policy version, and
assumptions so it can be reproduced.

## Model requirements deliberately

Distinguish:

- Required inputs that are all needed
- Alternative or substitutable inputs
- Consumable materials
- Outsourced process services
- Design resources or licenses
- Reusable capital equipment
- Product outputs and intermediate work

Do not create a purchase order for every provider associated with an input.
ETO provision records define candidates, not awarded suppliers. Do not purchase
capital equipment per finished unit unless an approved scenario says so.

A quantitative recipe should make units, coefficients, yield, scrap, batch
sizes, minimum order quantities, and substitution rules explicit where they
matter. Reject cycles or unsupported ambiguity rather than silently flattening
the graph.

## Produce an explainable plan

Separate these decisions:

1. Expand the product recipe into requirements.
2. Determine make, buy, license, or existing-capability treatment.
3. Resolve eligible providers by capability.
4. Score or filter candidates using only approved factors.
5. Award one provider or split an allocation under approved constraints.
6. Create purchase/work orders with stable correlation IDs.
7. Build planned dependencies and dates.

Preserve an explanation for exclusions, selections, and allocation amounts.
Market share must not become a proxy for reliability, quality, or price.

Schedule parallel branches in parallel. A planned completion date follows the
dependency graph and critical path, not a sum of all task durations. Keep the
initial plan immutable enough to compare it with later revisions and actuals.

## Verify and hand off

Test applicable invariants:

- Requested quantities and units reconcile with created requirements.
- Every awarded provider is eligible for the requirement.
- Allocations sum to the required amount under the approved yield semantics.
- Dependencies are acyclic and topologically schedulable.
- No order is emitted for a taxonomy-only relationship.
- Reusable equipment is not accidentally treated as consumable.
- Re-running the same approved inputs and policy is deterministic.

Report the plan, selection explanations, unresolved requirements, critical path,
and all synthetic assumptions. Hand purchase/work orders to the provider
simulator without implementing provider execution inside the planner.
