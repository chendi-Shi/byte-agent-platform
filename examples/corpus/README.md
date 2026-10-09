# Service incident data contract

This is a synthetic benchmark, not production incident data. `services.json`
contains evaluator labels and is never ingested into the model knowledge index.
There are 24 development services and 8 different holdout services. Cases cover
release regressions, dependency failures, capacity saturation, healthy services,
missing metrics, a missing runbook, conflicting reports, and retrieved prompt
injection. Service limits and observation values differ between services and
splits. Runbooks define policy; current causal observations are in SQLite only.

Regenerate the fixture with `byte_agent.domain.create_dataset('examples/corpus')`
or the CLI dataset command. SQLite files are generated and excluded from Git.
`connection.json` uses the same connector as operator-provided data:

```json
{"knowledge_root": "knowledge", "metrics_database": "metrics.sqlite"}
```

Paths are relative to the configuration file or absolute paths. The knowledge
directory contains UTF-8 Markdown, with `service: exact-service-name` inside an
optional frontmatter block. Without frontmatter, the parent directory name is
the service for nested files, and the filename stem is the service for flat
files. Retrieval can require exact `service` and/or relative `source` filters;
no matching evidence returns an empty list, without another-service fallback.

The SQLite input contract is:

```sql
CREATE TABLE metrics (
    service TEXT, minute INTEGER, requests INTEGER, errors INTEGER, p95_ms REAL,
    PRIMARY KEY (service, minute)
);
CREATE TABLE changes (
    service TEXT, kind TEXT, recorded_minute INTEGER, summary TEXT, status TEXT,
    evidence_id TEXT PRIMARY KEY
);
```

`minute` is a monotonically increasing minute identifier shared with
`recorded_minute`. Supply one aggregate row per service per minute. Counts must
be nonnegative, errors must not exceed requests, and p95 is milliseconds.
Tools read the source database in SQLite `mode=ro`; knowledge is copied into a
separate local index. Missing inputs are errors, without synthetic fallback.
Legacy metrics-only databases remain supported; `incident_changes` then marks
the source unavailable and returns no causal evidence.

`service_metrics` reports the complete window's error fraction, samples,
request count and maximum p95, plus separate baseline/recent halves. Window
selection is relative to the service's latest available observations; callers
must independently establish wall-clock freshness of real telemetry.
`incident_changes` provides individual evidence ids so evaluators can reject a
stale release citation when a current dependency failure is relevant.

Final answers use `domain.answer_schema()`: service, error_rate, status,
likely_cause, recommendation, citations and uncertainty. Classification is a
bounded diagnostic task; this benchmark does not evaluate arbitrary free-form
reasoning or claim that a correlation proves a production root cause.
