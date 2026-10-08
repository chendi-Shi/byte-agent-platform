# Search service runbook (synthetic)

Search error rollback: when search error rate exceeds 2% after a release, compare the release time with the first error spike. Inspect dependency health and request traces before proposing rollback. Obtain operator approval for any production action.

Search latency: p95 above 400 ms warrants checking downstream timeout budgets and index freshness. Error rate alone cannot establish the root cause. This example is synthetic and contains no company data.
