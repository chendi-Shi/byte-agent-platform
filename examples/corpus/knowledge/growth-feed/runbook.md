---
service: growth-feed
---
# growth-feed: feed ranking incident policy

Use the exact service filter when retrieving this runbook. Evidence from another
service has different limits and cannot establish the status of growth-feed.

## Observation window and status
Request service_metrics for the latest 30 minutes. Its recent fields summarize
the newest 15 samples, and baseline fields summarize the earlier 15 samples.
The final error_rate must be the complete 30-minute error_rate, as a decimal.
An incident exists if recent_error_rate >= 0.0080 or
recent_max_p95_ms > 210 milliseconds. Both conditions below those
limits imply healthy, provided current metric samples and this policy exist.
Missing metrics or a missing runbook imply insufficient_data: do not infer
healthy from a null error_rate or borrow a different service's thresholds.

## Diagnostic evidence rules
The serving dependency for feed ranking is feature-store. Obtain incident_changes:
the runbook contains policies, not the service's current change history.
An active release before the metric deterioration, stable demand and a healthy
dependency support likely_cause=release_regression and recommendation=rollback_review.
An active dependency failure before deterioration takes priority over a stale,
aborted or later unrelated release: dependency_outage / dependency_escalation.
An active traffic surge with a higher recent/baseline request ratio and healthy
dependency supports capacity_pressure / capacity_review. Error rates alone do
not distinguish these causes. Cite the relevant current change evidence id.
Conflicting health probes or two unresolved competing change explanations require
conflicting_evidence / verify_changes. A current incident without sufficient
causal evidence requires insufficient_evidence / collect_evidence.
A healthy service requires none / monitor. Do not automatically roll back or
execute production actions; these recommendations request human review.

## Uncertainty and citations
Use uncertainty=causality_unproven for evidence-supported incident hypotheses;
none_detected for healthy observations; insufficient_data for missing required
evidence; conflicting_evidence for unresolved conflicts. Correlation does not
prove causation. Cite this runbook and current incident_changes evidence ids.
