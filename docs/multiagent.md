# Durable Multi-Agent diagnostic workflow

`byte_agent.multiagent.Coordinator` adds four capability-isolated model agents.
Each has its own `Runtime`, system prompt, model context, SQLite journal, writer
lock, tool registry and answer validator. Agents may use the same installed model
weights; they do not reuse a single conversation or a previously generated answer.

| Agent | Granted tools | Responsibility | Output |
| --- | --- | --- | --- |
| Metrics specialist | `service_metrics` only | Inspect the complete 30-minute window, baseline and recent measurements | Actual measurement summary; causal hypothesis remains `insufficient_evidence` |
| Knowledge/change specialist | `knowledge_search`, `incident_changes` only | Read the target service policy and current causal observations; distinguish stale records | Findings, tentative hypothesis and actual citation ids |
| Reviewer | `read_shared_evidence` only | Independently critique both proposals against sealed original observations | Agree, disagree or insufficient, with a reason |
| Arbiter | `read_shared_evidence` only | Combine original observations, proposals and review into the existing diagnostic answer contract | Seven-field diagnostic JSON |

The coordinator uses fixed routing: specialists run sequentially, followed by
review and arbitration. This makes the CPU demonstration reproducible and avoids
four concurrent requests fighting for one local inference server. It does not
claim dynamic task decomposition, role discovery or parallel speedup.

## Evidence transfer and validation

Only completed specialist runs enter the shared evidence packet. The packet
contains their actual tool events and separately labelled model proposals. It is
canonical JSON, identified by a SHA-256 digest and stored under
`team/shared/<digest>.json`. The reviewer reads the first sealed packet. The
arbiter reads a second packet that also contains the review. A read tool returns
a fresh decoded copy, so a caller cannot mutate another agent's memory. The
coordinator checks stored digests on resume and rejects missing or altered
packets; this is integrity checking, not protection against an operator who can
modify the whole filesystem and coordinator database.

Service names originate with the operator. The specialist tools retain the
existing service-enum scope and reject cross-service attempts before original
handlers access data. Specialist validators require each role's expected tool
observations, bounded output fields and observed citations. Review and arbitration
must actually call their granted snapshot tool; the operator binds its exact
digest before execution.

Revision `durable-four-agent-v2-narrow-capabilities` narrows the model's capability
space after a recorded development failure. The first real run supplied
`incident_changes(limit=-1)` and then shortened an observed citation id; the
invalid tool attempt was correctly rejected and the team stopped. That run is
preserved as a failure. The new role schemas remove choices already determined
by the operator: metrics always use 30 minutes, search returns at most 3 results,
changes at most 20 records, and the snapshot reader takes no model arguments.
The knowledge role also cannot override the source selector; all retrieval
remains service scoped. A model that explicitly supplies a removed field is
rejected before source access, even if it guesses the operator's fixed value.

The binding wrapper validates the narrow request first, then adds trusted
arguments for the original tool. The raw provider message retains the original
request while durable tool events record effective `window_minutes=30`, limits
and snapshot digest. A pending request is committed before binding, so crash
replay performs the same validated operator binding. Tool revisions and team
identities bind this configuration. This is a development change to the action
space, not model training, an erased failure or relaxed evidence validation.
Citation ids still must be copied exactly from actual tool observations.

The arbiter's answer is passed to the existing `validate_diagnostic` against
original specialist observations, not against other agents' prose. This checks
service, measurement provenance, the aggregate rate, output schema and citations.
It does not establish whether a causal hypothesis is true. An additional
coordination policy detects distinct declared specialist hypotheses, excluding
`insufficient_evidence` and `conflicting_evidence`. A declared specialist conflict
requires `conflicting_evidence / verify_changes / conflicting_evidence` in the
final answer. A reviewer's `disagree` verdict alone does not establish such a
conflict: the arbiter must reconsider the original observations and the critique.
The coordinator preserves both the review and actual specialist conflicts and
lets the arbiter repair within its existing budget. This conservative policy
can trade recall for caution and needs separate evaluation before broader use.

The metrics role has no policy, change or dependency capability. It therefore
cannot establish a cause: its `hypothesis` is always `insufficient_evidence`, both
in its output schema and validator. This is a capability boundary for every
scenario, not an incident label. Its measurement summary still comes from an
actual model call. A knowledge proposal or review can establish a tentative
causal interpretation; a reviewer must not mistake the metrics role's limited
authority for a contradiction.

## Structured final-output phase

Revision `durable-four-agent-v3-structured-finals` adds `RoleOllama`. A second
preserved real development run acquired valid 30-minute metrics but repeatedly
returned malformed JSON with a `citations` key and no value. It exhausted its
role step budget and is retained as a failure.

Native tool calls remain enabled until each role's required successful tool
observations are present in actual `tool` messages. Missing, failed or
wrong-service observations cannot advance the phase; a snapshot must match its
digest. Once acquisition is complete, the request supplies the role's JSON Schema
as Ollama's `format` and disables tool selection for that output request. The
static contract contains required fields, types, generic enums and the operator's
service. V7 then binds citation choices to the exact IDs already returned by
successful, scoped tool observations or a verified sealed snapshot. The model
chooses among those observed IDs; it still must select relevant evidence and
justify a diagnosis. No expected cause, recommendation, gold label or measured
error rate is supplied as a schema answer. The independent validator continues
to check citations and their relation to the actual observations.

The static schema hash, citation-binding policy, role and phase rule bind the
generation configuration and run identity. Each model event records
`native_tools` or `json_schema`, the actual active schema hash, observed citation
count and the provider's original usage and timing. The static contract hash is
not required to equal the later evidence-bound schema hash. Structural decoding
does not validate causal truth or invent a result; independent validation and
ordinary repair budgets still apply. The final reviewer reason and specialist
summary prompts describe their responsibilities without placeholder answer text.

## Observation consistency and grounded review

`durable-four-agent-v6-observation-consistency` addresses two later development
failures: treating a specialist's limited permissions as globally missing data,
and confusing whole-window request counts with growth in demand. Sealed packets
now include `derived_facts` recomputed from successful target-service tool events.
These facts report actual aggregate availability, returned sample counts, the
baseline/recent row split at `samples // 2`, requests per observed sample, and
their ratio. They are neither generated by a specialist nor copied from its
prose. No sampling interval is inferred: these counts must not be labelled RPS
or requests per minute.

The knowledge agent's lack of metrics permissions does not remove metrics from
the shared packet. Review and arbitration inspect original observations and
derived facts before proposals. Prompts distinguish untrusted text from data
availability, working hypotheses from proved causality, and recommendations
requiring human review from actions already executed.

The arbiter additionally rejects a narrowly checkable unsupported
`capacity_pressure` hypothesis. This rule applies only when the actually
retrieved policy states that a traffic surge supports capacity pressure, the
metrics are complete and internally consistent, and the available change list
has not reached its result cap. A supporting interpretation needs observed
requests-per-sample growth and a cited current active traffic record. Otherwise
repair feedback contains the actual sample counts, counts/ratio and cited
traffic availability; it supplies no replacement diagnosis and reads no task
labels. Unrecognized policies, missing fields, partial windows or capped change
lists do not create evidence. Genuine growth with a cited active traffic record
remains permitted. This is bounded observation consistency, not a general
causal oracle or a completed accuracy comparison.

The V6 four-file immutable checkpoint and independent raw-byte checks are recorded
in [`acceptance-source-v6.json`](../examples/acceptance-source-v6.json). The same
checkpoint includes bounded retries for read-only Ollama model metadata;
inference requests are not automatically retried. Earlier V4/V5 sources and
real-model failures remain separate evidence. Actual Multi-Agent and network
outcomes are reported in [`v3-results.md`](../examples/v3-results.md), separately
from source identity and engineering tests.

V6 also exposed a reviewer failure: it used the tool name `service_metrics` as an
unobserved citation ID. Repeated repair attempts exhausted its bounded model-step
allowance before arbitration.
`durable-four-agent-v7-grounded-review` adds sorted `available_citation_ids` to the
sealed packet, derived solely from actual observations, and binds the active
output schema to those IDs after evidence acquisition. It retains the raw
observations, proposals and review. Reviewer disagreement alone is a request to
reconsider a proposal; it does not create a conflict between two actual
specialist hypotheses. A recorded specialist conflict still requires a
conservative final decision. The change has separate
[`acceptance-source-v7.json`](../examples/acceptance-source-v7.json) provenance.
The unsuccessful V6 attempt remains alongside the later attempt, with its actual
status and semantic checks; source identity or structured JSON does not by
itself establish task success.

The completed V7 run used 9 model steps, 5 tool calls and 11,088 known tokens
over 511.455 seconds. All four roles, active schemas, exact citations and sealed
observations passed their execution checks. The post-execution development
decision did not pass: service, aggregate error rate and incident status were
correct, but the arbiter returned `insufficient_evidence / collect_evidence /
insufficient_data`. It had first proposed a recent-window rate and an unsupported
capacity hypothesis; deterministic feedback rejected both, after which it became
overly conservative. The knowledge role's unsupported healthy proposal was
correctly challenged by the reviewer, but the final diagnosis still failed.
All seven development attempts are preserved. No further prompt changes were
made to obtain a passing answer on that same case. The
[published V7 report](../examples/experiments/multiagent-v3-compact/report.json)
records `runtime_completed=true` separately from `semantic_success=false`.

## Budgets, recovery and failure rules

Default team limits are 20 model steps, 16 tool attempts, 64,000 known input/output
tokens, 60,000 context characters per role and 1,800 wall-clock seconds. Each role
also has limits of 5 steps, 5 tool attempts and 16,000 known tokens. Its remaining
team allowance is calculated and persisted before execution. Validation repairs
consume ordinary model steps and tokens. Token usage is supplied by the provider;
unknown usage is counted explicitly, not estimated as zero. The token threshold
is checked after a response, so one in-flight response can cross the threshold;
provider output-token limits also bound that response.

A coordinator writer lock prevents concurrent writers for the same team run.
Completed role traces are committed before the next role begins. A process crash
after a role finishes but before the coordinator records it resumes that role's
durable journal without reissuing its completed model calls. A lost provider
response is `uncertain` and stops the team, with no automatic retry. A failed
required role stops downstream agents and leaves the final answer empty. There
is no fallback that silently marks a partial single-agent run successful.

Cancellation is checked between model/tool operations and roles. The wall-clock
deadline spans coordinator restarts. The Ollama adapter's request timeout is
capped by the remaining deadline; a request already in progress is subject to
that provider timeout. Arbitrary non-cooperative Python model/tool implementations
are not forcibly preempted. OS clock changes can affect elapsed time across
restarts. The output records `cancelled`, `timed_out`, `uncertain`, `role_failed`,
`budget_exceeded` or `evidence_error` rather than a fabricated completed answer.

## Run the separate development demonstration

Install this project and start the Ollama service. On a fresh installation, download the public
[`qwen3:4b`](https://ollama.com/library/qwen3:4b) model and create the local variant:

```shell
python -m pip install -e .
ollama pull qwen3:4b
python -m byte_agent prepare-model --model qwen3:4b --target qwen3:4b-instruct
python examples/multiagent_demo.py --model qwen3:4b-instruct --context 8192 --output-tokens 512 --provider-timeout 900 --wall-seconds 5400 --output runs/multiagent-real-v3-new
```

The `qwen3:4b-instruct` label is created locally by `prepare-model`; it is not a
published registry tag to pull. Use `ollama list` and the saved digest/template
metadata to verify the installed identity. If the tested label already exists, verify it rather than replacing
it during an experiment. Preparation preserves the original model weights; it
is a template compatibility change, not SFT/RL. Use a new output directory for
a different model identity or revision.

To exercise only engineering behavior:

```shell
python examples/multiagent_demo.py --fixture --output runs/multiagent-fixture-v3
python -m unittest discover -s tests -p test_multiagent.py -v
```

The script creates a new fictional `demo-upload-v3` development scenario and
reads no existing benchmark manifest or holdout labels. It saves model digest,
generation settings, source hashes and dataset hashes in `experiment.json`, plus
four individual traces, both evidence packets and `team/team-trace.json`. The
default real mode calls Ollama separately for every agent. `--fixture` uses
scripted decisions and is explicitly excluded from model-performance claims.
Changing model settings, tools, task or implementation requires a new output
directory because durable run identities bind them. The example dataset is
synthetic and cannot establish performance on production incidents.

## Design sources and boundaries

Anthropic describes separate orchestrator/worker and evaluator/optimizer patterns
and recommends adding complexity when there is a clear use case. This project
uses a fixed specialist/reviewer workflow; its coordinator does not perform the
dynamic LLM task decomposition described in that source.
[Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
informs the separation of work and review.

Ollama's documented chat tool support supplies tool schemas to each model request.
This workflow grants different schemas to different roles and uses native
provider tool calls rather than parsing pretend calls from free-form role-play.
[Ollama tool support](https://ollama.com/blog/tool-support)
describes the provider mechanism.

Ollama documents supplying a JSON Schema in the chat request's `format` field.
The role adapter applies this mechanism only after tool acquisition.
[Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs)
describes the provider feature.

Neither four roles nor a reviewer guarantees higher accuracy. The workflow adds
latency and tokens, shares model biases when using the same weights, and has a
bounded shared-context capacity. It is a concrete Multi-Agent implementation and
a reproducible development demonstration; comparative accuracy claims require
new, separately held-out tasks and fair single-agent baselines.
