# Persistent business storage

Actors use mounted files directly. There is no per-job copy/sync of memory or
workspaces. The control plane and all authorized actors must see the same files.
The filesystem must support atomic rename, fsync and POSIX advisory locks;
NFSv4 is the supplied deployment example. Back up this storage separately from
Substrate snapshots. The control plane's `jobs.db` stays on its own persistent
local/block volume, not NFS; run one API process per job database.

Enable `ExternalVolumes` on the Substrate server and use `kubectl-ate --preview`
when creating the template. Register the CSI driver as described in the pinned
[upstream guide](https://github.com/agent-substrate/substrate/blob/a46464663fa076221ae059e1fd4a273edd2d8b09/docs/guides/csi-volumes.md).
Apply a substituted copy of `storage.example.yaml`. The NFS driver's
[`subDir` and `onDelete: retain`](https://github.com/kubernetes-csi/csi-driver-nfs/blob/master/docs/driver-parameters.md)
keep shared directories independent of actor deletion. Kubernetes reclaimPolicy
alone does not do that: Substrate invokes CSI directly. Pin and validate your
CSI driver version before deployment; this migration tests mounts by simulation.
Provision export ownership for image UID 10001, without world-writable exports.

The shared-scope template mounts `shared` at `/state`, `locks` at `/locks`, the
workspace at `/workspaces/code`, and a unique journal volume at `/data`. Prepare
`shared` with the public sample teams and an actor-only `config.json` containing:

```json
{"data_dir":"/state", "storage_lock_dir":"/locks", "control_plane_url":"http://companest-api:8000"}
```

Put `example-storage-v1` in `locks/.companest-storage-id`. In the workspace,
put `code-v1` in `.companest-storage-id`. The actor's `workspaces.json` is:

```json
{"code":{"path":"/workspaces/code","storage_id":"code-v1"}}
```

The control-plane workspace declaration uses its own mount path and the same
storage ID. Company workspaces must also declare `company_id`. Unowned legacy
workspaces belong only to the shared scope. Every dispatched workspace requires
an identity marker; missing/wrong mounts fail closed. The actor verifies the
expected storage and workspace identities before running tools. A nonblocking
filesystem lock rejects concurrent writers to the same workspace. Memory
read-modify-write operations use a shared reentrant filesystem lock.

For a company template, mount only that company's directory at
`/state/companies/<company_id>`, the explicitly allowed shared team directories
at `/state/teams/<team_id>`, its generated CEO team at
`/state/teams/company-<company_id>`, and that company's workspaces. Use separate
StorageClasses with fixed retained subdirectories for those mounts. Mount only
actor configuration, never the control plane's config/credentials/jobs database.
The `/locks` export carries only lock files and the storage identity. All views
use that same lock export. Company config, memory seed and team declarations
therefore update in place and are read afresh by each job. Do not expose other
company trees through a parent mount or a symlink. Startup rejects visible
foreign company/team/workspace declarations, but filesystem/network isolation
must be enforced by operator mounts and Substrate policy, including for Bash.

The sample image no longer provides mutable business state. Golden snapshots
are disabled for these templates so each process reads current declarations.
The per-actor external journal is intentionally distinct from business storage:
a cold restart sees the accepted task and never replays unknown side effects.

Budget decisions and estimated spend remain on the control plane. Actors call
`/internal/jobs/<job_id>/authorize` with a per-job capability (never the API
admin token); terminal/cancelled jobs lose access. Estimates are charged before
execution and retained on failure/cancel, so concurrent actors cannot reuse the
same remaining budget. This remains estimated billing, not provider usage.
Only the job's company teams are permitted. Do not mount the finance ledger in
company actors. Schedule tool calls use the same scoped channel and retain their
company/user identity in the control-plane database. Allow actor egress to this
private API URL; use HTTPS outside a trusted private mesh.
