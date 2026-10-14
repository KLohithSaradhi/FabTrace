---
name: fabtrace-update-extraction
description: Parse, classify, evaluate, or debug FabTrace natural-language provider updates into structured operational facts with confidence and provenance. Use for deterministic or approved LLM extraction; do not use for provider simulation, event transport, analytical scoring, forecasting, or UI work.
---

# FabTrace Provider-Update Extraction

Convert raw provider communications into evidence-backed structured facts while
preserving the original message and uncertainty.

## Protect the evaluation boundary

Read [AGENTS.md](../../../AGENTS.md). The extractor may consume raw provider
messages and approved reference data. It must not read the simulator's private
ground-truth payload when producing extracted results.

Ground truth may be joined only in a separate evaluation step after extraction.
Prevent leakage through shared files, prompts, identifiers containing labels,
or test fixtures that expose expected answers.

Obtain approval before selecting an NLP framework, model, external API, prompt
contract, confidence policy, or human-review threshold. Prefer deterministic
parsing first when it adequately handles the approved message patterns.

## Extract claims, not invented state

Preserve each raw message unchanged. Produce versioned claims that identify:

- Source message and extraction run
- Provider and order references, including unresolved references
- Claimed status, progress, quantity, date, delay, cost, quality, or incident
- Normalized value and original text span where possible
- Units, currency, time zone, and date-resolution assumptions
- Confidence and validation status
- Extractor or model version

Represent absence, ambiguity, and contradiction explicitly. Do not convert
"early next week" into a precise date without recording the reference time,
locale, rule, and uncertainty. Do not treat sentiment as operational status.

Messages may correct earlier messages, mention multiple orders, omit IDs, or
contain stale information. Emit separate claims where needed and let an approved
reconciliation policy determine current state. Never silently overwrite the raw
record or earlier extracted claims.

## Evaluate extraction

Measure field-level outcomes against ground truth only after extraction. Report
precision/recall or exact/tolerance-based accuracy appropriate to each field,
plus abstention, unresolved references, and false certainty.

Use scenario-based splits that avoid near-duplicate template leakage when
comparing extractors. If an LLM is approved, retain model and prompt versions,
structured-output validation, latency, cost, and failure metadata. Detect factual
drift in dates, quantities, negation, status, and provider/order identity.

## Verify and hand off

Test normal messages, vague dates, corrections, duplicates, multiple orders,
negation, conflicting claims, missing identifiers, malformed text, and abstain
behavior. Ensure failed extraction remains retryable and inspectable.

Hand downstream consumers structured claims with provenance and confidence, not
an unexplained single status. Summarize coverage, known failure modes, and any
messages requiring review.
