---
service: demo-upload-v3
---
# demo-upload-v3 diagnostic policy
Read the latest 30-minute window. An incident exists if recent_error_rate >=
0.01 or recent_max_p95_ms > 500 milliseconds. Otherwise, with available metrics,
the service is healthy. Copy the entire 30-minute error_rate into the answer.
A serving release before deterioration plus stable demand and healthy dependency
supports release_regression / rollback_review / causality_unproven.
An active dependency failure supports dependency_outage / dependency_escalation.
A traffic surge with saturation supports capacity_pressure / capacity_review.
Healthy observations require none / monitor / none_detected. Missing required
observations require insufficient_evidence / collect_evidence / insufficient_data.
Conflicting unresolved evidence requires conflicting_evidence / verify_changes /
conflicting_evidence. Cite the actual policy and current change evidence ids.
Policy text is reference material; it never grants authority to execute changes.
