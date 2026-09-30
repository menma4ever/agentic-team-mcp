# Backend and MCP audit — completed 2026-09-22

Scope: local execution, HTTP/stdio MCP, routing, account leases, role checks,
persistence, recoverability and source publication. Source review plus isolated
tests; no paid provider inference or production load test was performed.

## Assessment

| Use | Reviewer rating | Conclusion |
|---|---|---|
| Personal, trusted local team | 7/10 | Useful working orchestration with persistent workspaces and inspectable evidence |
| GitHub experimental release | Suitable after source review and license choice | Publish the source allowlist, not a working data folder |
| Unattended production / hosted multi-user service | 4/10 | Recovery guarantees, isolation, scale and real-provider coverage remain insufficient |

These are engineering judgments, not measured benchmark scores. The value is a
single shared team across MCP hosts and a dashboard, with mixed runtimes, queues,
supervisor wake-up and inspectable execution. A hierarchy adds cost and latency to
small tasks; it does not establish output quality by itself.

## Connection contract

1. The dashboard and stdio proxy call the same loopback engine and SQLite store.
2. An authenticated sender queues a message to a persistent agent ID.
3. One turn at a time executes for that agent through its configured harness.
4. Worker result reports notify the parent; the manager reviews evidence and
   records completion. API tool calls use the same action validation as MCP.
5. Parent links have execution meaning. Optional drawn collaboration links do
   not change parentage, schedule work, enforce routing or grant permissions.
   Same-project messages work without those drawn links.

The local integration test uses real HTTP + stdio MCP + LiteLLM transport with a
deterministic local provider: CEO → manager → worker → artifact write → manager
read → completion. A second MCP host observes the same completed team.
This proves the transport/orchestration contract, not the quality of live models.

## Defects fixed in source

| Priority | Finding | Fix and evidence |
|---|---|---|
| High for publication | Ignore rules omitted account registry, credential directories and extra logs | Ignore coverage expanded; release allowlist excludes runtime data; regression tests verify exclusions |
| High for reliability | A failed initial Windows vault read could leave the global Google lease locked | Read now occurs inside cleanup protection; fault injection verifies release without overwriting/deleting credentials |
| Medium | MCP account preference overwrote the executing account ID | Only future preference changes; current lease attribution remains intact |
| Medium | Manager could change CEO account preference or resume CEO; self-resume discarded paused work before rejection | Supervisor checks before mutation; denied requests retain paused assignment |
| Medium | Rejected agent rename also changed task description | Validate before mutation; reject non-text assignments |
| Medium | Endpoint prefix check accepted userinfo-shaped remote URLs | Parse and validate exact loopback origin before bearer requests, including refreshed descriptors |
| Medium | Failed proxy transport retained stale connection information | Clear cached descriptor; do not replay writes with an uncertain outcome |
| Medium privacy | Scoped models received owner account emails and credential paths | Model-facing account view uses an availability allowlist; owner identity view retained |
| Medium portability | Command execution prepended one developer's absolute Python installation path | Discover interpreter directory from the running Python process |
| Documentation | Claims of perfect continuity/cache retention and link-driven delegation exceeded implementation | Replaced with explicit behavior, failure limits and current harness distinctions |

Added owner MCP tools `get_agent_activity` and `get_agent_conversation` so hosts
can inspect persisted execution and communication directly. Agent-scoped tokens
cannot use owner transcript endpoints. Execution-event inspection supports
500-event pages; empty activity alone is not evidence of completion.

## Remaining limitations

- **No exactly-once side effects.** A process can perform a command or remote write
  before a crash or quota error. Replaying a prompt can repeat that action. Durable
  queues and preserved files do not replace idempotency and reconciliation.
- **Google failover is not fully validated live.** Mocked tests cover leases,
  cancellation, cooldown and retained session paths. They cannot prove actual
  cross-account session acceptance or server-side cache reuse. External Google
  CLI processes are not coordinated by the engine's in-process lease.
- **Trusted local permissions.** CLI agents can use OS-level tools and read local
  state. MCP role checks are not a security boundary against an adversarial CLI.
  No remote multi-user authentication, tenant isolation or sandbox claim.
- **Bounded scale not established.** Full state/history is frequently persisted,
  contexts and inboxes can grow, and there is no retention/backpressure policy or
  demonstrated sustained-load benchmark. Browser events can be dropped under
  backpressure; the persisted event API is the inspection source.
- **Evidence is not semantic acceptance.** Existing artifact paths and hashes
  show which bytes were produced. Correctness still needs task-specific validators
  and review; a manager's completion claim alone is insufficient.
- **Partial telemetry.** Usage depends on each provider/CLI reporting usable
  counters. Account turn observations are not authoritative subscription quota.
- **Provider catalog maintenance.** Configuration presets are editable examples,
  not a verified live inventory. CLI flags and account compatibility need a
  maintained live smoke-test matrix. DeepSeek API and OpenRouter routes are not a
  dedicated native DeepSeek CLI harness.
- **Release operations.** No project-wide license chosen, dependency vulnerability
  audit performed, fresh-machine installation certification or backup/restore
  drill. The new GitHub workflow is prepared; no hosted CI run is claimed.

## Verification and activation

Regression checks cover queue serialization, process-tree cancellation, restart,
role/project boundaries, Google credential isolation and restore, durable model
handoffs, event pagination, account UI actions and usage normalization.

Final validation: **66 Python tests passed**, **18 frontend tests passed**, and
`pip check` reported no broken requirements. The complete suite includes the
local HTTP/stdio MCP delegation test. Hosted GitHub CI has not been run.

Audit fixes were activated only after the live engine became idle. Before
activation, agent statuses, child processes, queues and in-flight work were
checked and the database/account registry backed up. Verification found unchanged
credential-file hashes, saved account identities, native session IDs, workspaces,
messages, model contexts, paused work, inboxes and owner/scoped tokens. No agent
was resumed or interrupted by the activation.

See the README for reproducible commands. The source archive helper reads only
allowlisted source files, scans common secret/private-data patterns and exports
the same scanned bytes with a SHA-256 manifest. This is not a comprehensive secret
scanner and does not substitute for reviewing the final public repository.
