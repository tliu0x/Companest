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

Move the existing harness into an OCI actor workload and submit a real
Companest job through company authorization and CostGate. Persist a separate
actor reference alongside the business job ID. The Python transport in
`companest/runtime/substrate.py` calls this pinned CLI and uses its authenticated
loopback proxy. It fails on unavailable infrastructure and never retries
mutations after an ambiguous timeout. It is not yet wired into `JobManager`.
Finalize actor granularity and
workspace ownership with the task integration; counter actors do not settle
that choice. Verify reconnect, cancellation, ambiguous submission and company
isolation before deleting the local worker queue, instance cache or recovery
logic. Keep `handoff/` until the migration is complete.
