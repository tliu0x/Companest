
For Substrate execution, mount this data directory as described in
[the persistent storage setup](../../deploy/substrate/storage.md). Add explicit
workspace `storage_id` and `company_id` (for company-owned workspaces) to
`workspaces.json`; storage identity files must match on control and actor mounts.

Memory uses FileBackend. A model can create a categorized entry with these
`memory_write` arguments:

```json
{
  "key": "release-decision.json",
  "value": "{\"decision\": \"Run a staged rollout\"}",
  "category": "fact",
  "summary": "Release uses a staged rollout",
  "tags": ["release"],
  "expected_version": 0
}
```

The tool returns the original team_id/key and the new version. Read the entry
before updating it and supply that version as expected_version. memory_read
returns a bounded text page and next_offset; keep version fixed when paging.
Use memory_history and memory_restore when explicitly included in the Pi's
tool list (the archiver preset includes both). Job/Pi provenance is supplied
by execution context. See the [memory guide](../../companest/memory/README.md).
