# Persistent business storage

Actors use mounted files directly. There is no per-job copy/sync of memory or
workspaces. The control plane and all authorized actors must see the same files.
The filesystem must support atomic hard-link creation, rename and fsync;
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
CSI driver version before deployment. Provision export ownership for image UID
10001, including newly allocated journal directories, without world-writable
exports. The local kind check used NFS CSI v4.13.4 with mTLS and server-side UID
mapping for the provisioner. An NFSv4 `fsid=0` export is mounted as `/`, even
when its server-side directory is named `/exports`.

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
read-modify-write operations use the same shared locking protocol.

## Lock ownership and recovery

The pinned gVisor runtime's `flock` did not exclude another sandbox or the native
control process in the real kind check. Shared memory/workspace locks therefore
publish a complete owner record with an atomic hard link at `<lock>.claim`.
The record carries a unique acquisition token and, in actors, the unchanged
`job_id` and `company_id`. Normal release verifies ownership before unlinking.
Contention fails immediately for workspaces and after 30 seconds for memory;
this timeout never expires or steals a claim. Suspended actors retain ownership.

After Substrate confirms actor deletion, JobManager removes only that job's
claims. Before memory initialization, `companest serve` also checks recorded
claim owners against Substrate and recovers claims for confirmed missing actors.
An outage, a suspended actor or a terminal database row alone is insufficient.
The next managed memory access recovers any pending file commit normally.

Claims without a known job owner, including a control process killed during a
write, remain locked. Stop the relevant writers and establish that the owner has
terminated before removing that specific claim; never clear the lock directory
by age. If an actor crashed with a claim and still exists in Substrate, retire
that actor before restarting the API. This favors data integrity over automatic
availability after an uncertain crash.

The API's local/block `jobs.db` owner still uses a native kernel lock. Run the
control container on the native runtime, with one API process per database.
Drain old actors and stop old control writers before upgrading both images;
the `hardlink-v1` readiness/task handshake rejects incompatible new submissions.
Existing memory/workspace IDs and files are unchanged; old empty `.lock` files
are harmless, but old code must not remain active alongside the new protocol.

For a company template, never mount the complete control-plane data tree.
Substrate forbids nested volume mount points. Use disjoint mounts and an
operator-provisioned scope view containing only approved links, for example:

- `/scope`: retained company view with actor-only config, soul/user context,
  `workspaces.json`, `companies/acme -> /company`,
  `teams/general -> /shared-general`, and `teams/company-acme -> /ceo`.
- `/company`: the authoritative `companies/acme` directory.
- `/shared-general`: the authoritative, explicitly allowed `teams/general`.
- `/ceo`: the authoritative generated `teams/company-acme` directory.
- `/locks`, `/workspaces/code`, `/data`: as above, with a company-owned workspace.

Set the actor command to `--config /scope/config.json --company acme` and its
`data_dir` to `/scope`. Each mount has its own StorageClass selecting the existing
retained subdirectory, except the unique journal. Provision the symlinks/context
files in the scope view before launch; they are configuration, not copies of
mutable team/company memory. The control plane continues to use its original
canonical paths. Allowed team membership changes require updating the scope view
and template grants, not copying data into a new job image.

Mount only actor configuration, never the control plane's credentials or jobs
database. Do not mount the finance ledger into company actors. `/locks` carries
only lock files and the storage identity. All views use the same lock export.
Company declarations and memory update in place and are read afresh per job.
Never expose another company's tree through a parent mount or a symlink. Startup
rejects visible foreign declarations; filesystem/network isolation still depends
on operator mounts and Substrate policy, including when Bash is enabled.

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
