# Substrate development environment

This is the first migration gate: launch the actual upstream runtime and
exercise a stateful actor before changing Companest's execution path. The
Companest API still uses the existing execution path until that gate and the
agent task integration pass. Installing this environment alone does not migrate
jobs or establish production readiness.

`upstream-revision` pins the source used by the installer and CLI. The current pin is
[`a46464663fa076221ae059e1fd4a273edd2d8b09`](https://github.com/agent-substrate/substrate/tree/a46464663fa076221ae059e1fd4a273edd2d8b09).
Its `go.mod` requires Go 1.27.0 with toolchain 1.27.3. Go downloads the toolchain
when needed; Docker and kubectl must already be available.

## Launch and verify

From the Companest repository root:

```bash
bash deploy/substrate/bootstrap.sh
```

The bootstrap uses the upstream kind creation script and `ate-setup` installer.
It creates `companest-substrate`, writes a dedicated kubeconfig under the state
directory, builds for the node's CPU architecture, and installs only the gVisor
sandbox class. It also deploys upstream's counter demo and builds `kubectl-ate`.
Build concurrency defaults to two through `GOMAXPROCS`; override it explicitly
only when the development machine has enough spare CPU and memory.
It does not change the user's current Kubernetes context. Upstream's local
registry is named `kind-registry`; bootstrap refuses to replace an existing
registry or cluster whose ownership it cannot establish. The state directory
can be selected with `COMPANEST_SUBSTRATE_STATE_DIR`.

Follow the pinned upstream counter walkthrough to inspect an actor, request it
through a loopback proxy, suspend it, and request it again. The demo increments
memory, durable-directory and root-filesystem counters. This verifies the
runtime lifecycle only; it does not establish Companest job recovery, tenant
isolation or model access. Temporary simulation scripts are kept outside this
repository. Real model/tool acceptance is outside this migration's scope.

If the Docker Desktop credential helper blocks while pulling public images,
use a separate empty Docker configuration directory and explicitly set
`DOCKER_HOST` to the local daemon socket reported by `docker context inspect`.
Keep those settings in the calling shell; do not edit or copy the user's Docker
credentials into this repository.

## Verified upstream interfaces

The pin exposes `substrate.ateapi.v1.Control` in
[`api/ateapi/v1/ateapi.proto`](https://github.com/agent-substrate/substrate/blob/a46464663fa076221ae059e1fd4a273edd2d8b09/api/ateapi/v1/ateapi.proto).
Actor lifecycle is gRPC: `CreateActor`, `GetActor`, `SuspendActor`,
`ResumeActor`, `DeleteActor`. `Actor.status.state` describes execution
environment state; it is not a business job status. Actor templates are
Substrate resources; worker pools are Kubernetes CRDs. There is no assumed
Python SDK or REST actor endpoint in this integration.

The upstream `kubectl-ate proxy actor` uses mTLS and a CONNECT tunnel to
`atenet-router`; an incoming request can resume a suspended actor. API access
and router access have distinct authentication requirements. The development
install explicitly allows the upstream `atenet-client` identity. Production
control-plane access must use scoped identities and access policies rather
than the local CLI's shared bootstrap identity.

Kind snapshots use the upstream local object-storage service. These local
volumes disappear when the cluster is deleted; they are not a backup of
Companest company memory. Provider credentials belong in the upstream egress
credential provider, whose namespace policy starts with no grants. They must
not be baked into the actor image or bootstrap arguments.

## Next migration gate

The harness workload is built with:

```bash
docker build -f deploy/substrate/Dockerfile.actor -t localhost:5001/companest-actor:dev .
docker push localhost:5001/companest-actor:dev
```

Copy `actor-template.example.json` outside the repository and replace
`<ACTOR_IMAGE_DIGEST>` with the digest reference printed by the push. Create it
using the pinned `kubectl-ate create actor-template -f <manifest>` and the
dedicated kubeconfig/context. The example uses the upstream counter worker
pool and atespace, and packages only the public sample in the shared scope.

The workload serves `/readyz`, `POST /task` and `GET /task` on port 8080. A task
contains `job_id`, `task`, `company_id` and `context`; `context.team_id` selects
a team, and its absence uses existing automatic routing. The workload checks
its configured company scope before accepting a task and invokes the existing
orchestrator, including its team permissions, CostGate and tool restrictions.
The job ID is supplied by the business control plane and differs from the
Substrate actor name. This first workload uses one actor per job.

Repeated identical submissions return the same job record; a different job or
payload receives 409. Execution continues after the submitting HTTP request
returns. The journal is under `/data`, on an upstream `durableDir`. A memory
snapshot resumes the process. A cold process startup with a running journal
marks the job failed with an unknown outcome and never replays it. A terminal
result survives cold startup if the journal is present. Cancellation must
terminate the actor through Substrate; closing the HTTP client is not a cancel.

The workload listener requires an isolated actor network and trusted router
ingress. It is not a public Companest API and must not be exposed directly.
The sample's data is private to that actor. Shared company memory, writable
workspaces and dynamic company configuration still need their authoritative
storage boundary before the API can switch to this workload. Copying a mutable
company tree into each job image would lose updates and is not a migration.

Submit the packaged harness through the Companest CLI/API job path and preserve
company authorization and CostGate. Persist a separate
actor reference alongside the business job ID. The Python transport in
`companest/runtime/substrate.py` calls this pinned CLI and uses its authenticated
loopback proxy. It fails on unavailable infrastructure and never retries
mutations after an ambiguous timeout. It is not yet wired into `JobManager`.
Finalize actor granularity and
workspace ownership with the task integration; counter actors do not settle
that choice. Verify reconnect, cancellation, ambiguous submission and company
isolation before deleting the local worker queue, instance cache or recovery
logic. Keep `handoff/` until the migration is complete.

## Development checks recorded

On 2026-11-15 the pinned control plane and the counter demo launched on an
arm64 kind node. Through `SubstrateClient`, a counter request returned 1 for
memory, durable files and rootfs; after synchronous suspension a new request
returned 2 for all three, and deletion succeeded. The harness image also
started under gVisor and produced its golden snapshot.

Temporary simulations covered transport errors/timeouts and the existing
harness with its model call replaced: task submission, duplicate suppression,
company rejection, result retrieval and cold-start failure without replay.
The inherited suite passed with 523 tests and 3 live-network tests deselected.
These checks do not establish a completed API migration or real model/tool
acceptance, which is outside this task's scope.

## Persistent mounts

The current actor now requires persistent business mounts and storage identities;
see [storage.md](storage.md) and `storage.example.yaml`. The template uses CSI
volumes with retained shared directories and a separate per-actor journal. Create
it with `--preview` against a server with `ExternalVolumes` enabled. The image's
sample files are not the authoritative company/workspace state.

## Control-plane task path

`companest serve` now requires `substrate` configuration with an explicit
company-ID-to-atespace/template map (`@shared` for global work). See the current
minimal and production configuration examples. The job database resides under
`config.data_dir`. Only one API process may own it; SQLite is required by the
`server` extra. The API admin bearer token remains required outside debug mode.

CLI jobs, API jobs, team runs and gateway calls submit through JobManager. It
stores the actor reference and intent before creation, keeps full results, and
reconnects after API restart. It has no queue, execution workers or local runtime
fallback. Old unfinished records without actor references fail with an unknown
outcome instead of replaying. Ambiguous create/POST operations retain their job
ID and can be queried again; POST is not replayed after an uncertain response.
An unavailable execution plane leaves `execution.error` on the record and does
not assert that the task failed. Closing a client/API process does not cancel an
actor. Cancellation is acknowledged only after Substrate deletion succeeds;
failed deletion returns 503 and leaves the cancellation intent for reconciliation.
A terminal result is committed before the actor/journal is removed. Querying a
job reconciles its state; no background worker polls or re-executes jobs.

Budget and schedule callbacks use job-specific capabilities. They cannot use the
admin API or another job/company. Estimated spend is reserved before a team
executes, including nested delegation, and remains charged after cancellation.
