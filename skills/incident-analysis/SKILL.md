---
name: incident-analysis
description: Diagnose synthetic service incidents using bounded read-only tools.
---

1. Obtain service_metrics for the requested service.
2. Search runbooks with knowledge_search.
3. Cite returned evidence ids, separate observations from hypotheses, and state missing evidence.
4. Propose investigation steps. Production actions require an operator.

This file is a reusable workflow for a host that supports skills. The native CLI
uses the same workflow in its system prompt; it does not implement skill discovery.
