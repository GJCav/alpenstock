## Filesystem WAL Safety Follow-up

This note records the safety hardening pass after the JSONL WAL and lazy
filesystem lifecycle optimization.

### Bugs fixed

- WAL-backed local mutations must not update in-memory transaction state before
  the corresponding WAL record has been appended. Otherwise a WAL append
  failure can leave the process overlay ahead of recoverable metadata.
- Child coordination progress must follow the same rule: parent memory must not
  say a child is prepared or committed unless the parent WAL records that
  progress.
- Coordinated child repos must not be recovered directly through the public
  filesystem backend recovery entrypoint. Recovery is coordinator/root-driven so
  the parent WAL remains the authority for enrolled children.
- Parent/child transaction setup is a two-WAL handshake, so parent enrollment is
  treated as the authoritative coordination edge. Independent child `begin()`
  checks pending ancestor WAL metadata and refuses to start when an ancestor has
  enrolled the child subtree for recovery.
- Parent-driven recovery treats a missing enrolled child transaction directory
  as a no-op, because that state also represents the valid crash window after a
  child has committed and cleaned its local transaction state but before the
  parent has appended `committed=True`.
- Coordinated child begin cleanup is best-effort: if the child marker write
  fails, parent unenroll and child rollback are both attempted, and cleanup
  failures are attached as notes to the original setup failure.
- Child WAL records now require real boolean `prepared` and `committed` values
  instead of coercing arbitrary JSON values with `bool(...)`.

### Contract clarification

The lazy committed-state adapter intentionally does not freeze a full filesystem
snapshot at transaction begin. Filesystem transaction isolation assumes writes
under a repo go through the storage API while a transaction is active. Direct
external mutation of repo files during an active transaction is out of contract.

The filesystem backend remains scoped to program-kill recovery, not power-loss
durability. Normal lifecycle paths still avoid file and directory `fsync`.
