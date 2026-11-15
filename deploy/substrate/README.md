# Agent Substrate execution

Companest is the business control plane; Agent Substrate owns actor placement,
sandbox lifecycle, suspension and recovery. Every job uses one actor, identified
by a persisted atespace/name/UID reference separate from the business job ID.
There is no local worker queue or execution fallback.

The integration pins upstream
[`a46464663fa076221ae059e1fd4a273edd2d8b09`](https://github.com/agent-substrate/substrate/tree/a46464663fa076221ae059e1fd4a273edd2d8b09).
`upstream-revision`, the bootstrap and the control image must use the same pin.
The transport calls the official `kubectl-ate` CLI rather than inventing a REST
API or Python SDK. It uses the CLI's authenticated loopback actor proxy.

## Provision the runtime and storage

1. Install Docker and kubectl, then run `bash deploy/substrate/bootstrap.sh`.
   The script uses upstream's kind and ate-setup installers, creates an isolated
   `companest-substrate` cluster/kubeconfig, builds for the actual node CPU,
   enables gVisor and `ExternalVolumes`, and installs the CLI and counter demo.
   Go 1.27.0/toolchain 1.27.3 is required by the pin; the toolchain downloads if
   needed. Build concurrency defaults to two. The script refuses registry or
   cluster collisions rather than replacing someone else's environment.
   `COMPANEST_SUBSTRATE_STATE_DIR` selects its local state directory.
2. Configure the pinned upstream [CSI integration](https://github.com/agent-substrate/substrate/blob/a46464663fa076221ae059e1fd4a273edd2d8b09/docs/guides/csi-deployment.md)
   and retained storage using [storage.md](storage.md). The bootstrap does not
   provision company business storage. `storage.example.yaml` provides NFS
   StorageClasses; substitute the server and provision directory ownership.
   CSI support is an upstream preview feature. Ordinary actor volumes are
   deleted with the actor, so the retained directory configuration is required.
3. Build/push the harness and use its digest in a copy of
   `actor-template.example.json` outside the repository:

   ```bash
   docker build -f deploy/substrate/Dockerfile.actor -t localhost:5001/companest-actor:dev .
   docker push localhost:5001/companest-actor:dev
   kubectl-ate --kubeconfig <KUBECONFIG> --context <CONTEXT> create actor-template -f <MANIFEST> --preview
   ```

   The example selects the counter demo worker pool and shared scope. Production
   requires dedicated pools, company-specific atespaces/templates, scoped
   identities/policies, and only the permitted filesystem mounts. The image
   contains code only; actor config, company data and workspaces come from
   persistent mounts. Golden snapshots are disabled to read current declarations
   on startup. The `/data/task.json` journal has its own external per-actor volume.
4. Configure the API using `deploy/config.prod.md` or the minimal example. Set
   `data_dir` to persistent business state, `control_state_dir` to persistent
   local/block storage for SQLite, `storage_lock_dir` to the shared lock mount,
   and `substrate.storage_id` to its identity marker. Explicitly map each company
   ID to its atespace/template in `substrate.scopes`; `@shared` enables global
   jobs. The actor's config sets `control_plane_url` to the private API endpoint
   for budget, schedule and business event callbacks. Do not mount API admin
   credentials or the control database into actors.
5. Start `companest serve -c <CONFIG_PATH>`, or configure the required mount and
   kubeconfig environment variables for `deploy/docker-compose.yml`. One API
   process owns each job database. `server` installs required SQLite support;
   `scheduler` enables cron/interval/date business triggers. Gateway integration
   remains optional and is no longer a mandatory compose dependency.

The CLI's bootstrap identity is for local development. Production control and
router access require scoped identities and ingress policies. Actor port 8080
is reachable only through trusted Substrate routing, never a public unauthenticated
listener. The template supplies non-secret SDK placeholder keys and a projected
trust bundle. Configure HTTPS header replacement and grants for DeepSeek and the
sample Moonshot router using the pinned
[credential injection guide](https://github.com/agent-substrate/substrate/blob/a46464663fa076221ae059e1fd4a273edd2d8b09/docs/guides/egress-credential-injection.md).
Without these grants calls fail closed; placeholders are not credentials.
Provider credentials belong in upstream egress credential injection;
keep provider/tool destination grants limited to the company. The API still
requires `COMPANEST_API_TOKEN` outside debug mode; its bearer token is an admin
credential, not a tenant identity. The actor callback capability grants access
only to that live job's company, user and services.

## Submit, reconnect and cancel

```bash
companest job submit "Summarize the briefing" --team-id general --idempotency-key briefing-001 --json
companest job status <JOB_ID> --json
companest job cancel <JOB_ID> --json
```

Use `--company-id`, `--workspace-id`, `--mode`, `--cascade` and `--workflow` as
needed. The API equivalents are `company_id`, `context` and `idempotency_key`.
Retrying an idempotency key for the same company/client label and identical
request returns the same job; conflicting content is rejected. Without a key,
each submission intentionally creates a new job. Keep the returned job ID.

JobManager saves intent before creation and submission. A lost POST response
is reconciled through GET; an uncertain task request is never replayed. API
restart reconnects to the saved actor and does not create a replacement. Old
unfinished records without references fail with an unknown outcome. Queries
retain full results and expose transport failures in `execution.error` without
mistaking an unavailable execution plane for a failed job. Actor environment
states and business job states remain separate. An individual job query refreshes its projection; list/count/stats return the
last observed state without network fan-out during an outage. No background
execution/recovery worker is implemented.

The actor durably journals acceptance before execution. Identical task requests
are idempotent; a different job/payload gets 409 and a foreign company gets 403.
A memory snapshot can resume the same process. A cold startup finding a running
journal reports an unknown outcome and never replays it. Client/API disconnects
do not terminate actors. Budget callbacks require the API to be available;
additional work fails closed when authorization cannot be obtained.

Cancellation saves intent and asks Substrate to delete the actor. Only successful
deletion (or confirmed absence) yields `cancelled`; unavailable deletion returns
503 and subsequent queries retry the intent. External side effects are not
rolled back. A completed result is committed to SQLite before actor cleanup.
An unpolled completed actor remains queryable until reconciled/retired; use the
upstream lifecycle policy for idle sandbox resource management. Back up both
business storage and the control database. None of this promises exactly-once
tool/model effects.

## Business ownership after migration

| Area | Current owner |
| --- | --- |
| Jobs | Companest stores identity, scope, capability, full result and actor projection; Substrate executes. Local queue/workers/replay/eviction removed. |
| Team registry | Declarations and per-invocation harness construction. Instance cache, active counters and idle eviction removed. |
| Orchestrator | Company configuration, permissions, triggers, routing declarations and remote submission. Model/tool execution lives in `runtime/harness.py`. |
| Pi | Existing LangChain harness inside the actor; Substrate core does not replace model/tool business logic. |
| Company components | Business extensions and async-safe registration hooks retained; no actor placement responsibility. |
| Schedulers/background | Business cron/interval/date triggers retained. CEO, enrichment, user tasks and Dreamer submit through JobManager. |
| API/CLI/gateway | Existing admin auth, jobs/results, company operations and optional channel protocol retained; execution submits to Substrate. |
| Events | Business task/routing events return through job capabilities and retain event-history/WebSocket contracts; actor lifecycle is not duplicated. |
| CostGate | Control-plane authorization and estimated-spend reservation before team work, including failed/cancelled work. Actor never reads the cross-company finance ledger. |
| Memory/knowledge | Shared mounted files, Qdrant retrieval, Dreamer, public knowledge and S3 backups retained; snapshots are not domain backups. |
| Modes/workflows | Three modes; independent cascade strategy. Decomposition/delegation moved to bounded business workflows, without a new workflow runtime. |
| Deployment | Pinned upstream CLI in the control image; code-only harness image, explicit persistent mounts and scope templates. |

For configurations generated before migration, change `mode: cascade` to
`mode: default` plus `cascade: true`; `mode: loop` to `workflow: decompose`;
`mode: conditional` to `workflow: delegate`. Compatibility reads remain until
the next major release. Delegation knows its allowed team list, checks each
branch budget, permits at most five steps and rejects nested delegation.
`always_on` now describes meta-team routing; it does not retain a local process.
The old `skip_cost_check` flag is accepted for compatibility but has no effect.

Nested CEO/delegation/refresh calls inherit the accepted job's company, user
and workspace; they cannot switch to a different workspace inside the actor.
Actor callbacks cannot raise priority above the submitted job, and company
disablement/cancellation is checked again after budget approval waits.
Business interval callbacks wait for remote completion; actor execution owns
the deadline. One-shot user schedules are claimed in SQLite before dispatch,
so restart does not replay an uncertain occurrence. A crash between claim and
dispatch can leave that occurrence unexecuted; inspect the job records before
explicitly scheduling a replacement.

The Docker control plane also needs `COMPANEST_WORKSPACE_MOUNT`, mounted at
`/mnt/companest/workspaces`. Its workspace declarations must reference the same
persistent storage identities as the corresponding actor templates.

Company schedule names now use `company/<id>/<name>` and enrichment schedules
use `enrichment/<team_id>` without character replacement. Update operator
run-now references accordingly; company status uses exact scope metadata, not
ambiguous name prefixes.

### Existing Qdrant indexes

All teams now use one `companest_memory_v2` collection and the original `team_id`
as their scope identifier. No team ID is rewritten or mapped to a second team
identifier. Queries filter by exact `team_id`; record UUIDs include both that ID
and the memory key, so one team's write/delete cannot overwrite another's same
key. Old per-team collection names could alias private and shared teams, so
there is deliberately no fallback. File memory remains authoritative and unchanged.
For deployments using Qdrant, instantiate the configured `QdrantBackend` with
the authoritative memory mount and call `backend.reindex_team(team_id)` for
each registered team, using the fully qualified `company/team` ID for private
teams. This rebuild needs the configured embedding provider and Qdrant service;
the review only simulated it. Validate the new indexes before retiring legacy
collections. Deploy actors with company-scoped vector-service credentials where
available; application query filters are not a substitute for service ACLs.

## Verification scope

Earlier in this branch, the pinned control plane and counter launched in an
isolated arm64 kind environment; state survived synchronous suspend/resume and
deletion succeeded. The initial harness image also launched under gVisor. These
historical checks are not acceptance of the final storage/API deployment.

Per the requested scope, the remaining migration uses simulated transport/models
and temporary scripts outside the repository. Checks cover persistent memory
updates across processes, workspace identity/writer exclusion, real API + actor
harness flow, restart/reconnect, uncertain POST, cancellation/outage, full
results, central budget authorization and idempotent submission. The final
actor manifest is strictly parsed against the pinned upstream protobuf schema.
Existing contract tests were adapted; no new simulation script is shipped.
Real model/tool execution and final CSI/cluster deployment acceptance were not
performed. Run that acceptance separately before using this in production.

The migration initially passed 521 default tests. PR review then removed 39
duplicate/wrapper test functions (68 cases); the retained suite passed with
**453 tests and 3 live-network tests deselected**. Public API, isolation and
data-integrity contracts remain covered; see [test scope](../../tests/README.md).
Compilation and targeted static checks passed. Temporary boundary simulations
also checked company/capability denial, company/workspace schedule persistence,
actor UID replacement, malformed records and durable-result-before-deletion.

The PR review additionally simulated priority ceilings, revocation during
approval, cancellation-safe actor acceptance, nested scope inheritance,
one-shot schedule claims, hot schedule registration, and Qdrant read/write/delete
isolation using the canonical team ID with fake embeddings.

## Execution field contract

Execution uses one field name and value per entity across API, CLI, Python
services, actor payloads, events and storage:

- Jobs use `job_id` in submission, detail/list responses and the Job model.
  New actor resource names use that same ID without a `job-` rewrite. Saved
  references for existing actors remain valid.
- User schedules use `schedule_id` and the `Schedule` model. UserScheduler and
  ActorServices expose `add_schedule`, `list_schedules`, `cancel_schedule`.
  Cancellation requires the exact ID; new schedules use full UUIDs.
- Budget requests/estimates/approval metadata use `team_id` and `model`, replacing
  target_team/target_team_id/target_model. The sessions_send tool takes `team_id`.

Existing jobs.db and user_schedules.db primary-key columns migrate from `id` to
`job_id` / `schedule_id` once at startup; their values and actor references are
preserved. No runtime alias layer is kept. Update CLI/API consumers and actor
images together when deploying this branch; do not run mixed contract versions.

CompanyMemoryNamespace callers must pass the complete canonical team_id.
Declarations and legacy seed directory names resolve once during configuration
initialization; runtime memory methods only validate the ID and pass it through.
TeamConfig stores the physical source_dir separately instead of keeping a second
local_id and mutating the team ID after creation.
