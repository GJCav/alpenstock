## Rev 2 Coordination Review Follow-up

This note records the bugs found during the follow-up review focused on nested
transaction coordination and transaction lifecycle.

### Bug 1. Deep nested SQLite recovery loses grandchildren

- Symptom: recovery of a prepared nested tree in SQLite can leave a grandchild
  repo transaction stranded while still committing its ancestors.
- Root cause: child participant paths are stored relative to their parent repo,
  but SQLite recovery was recursing using the raw relative child path instead of
  joining it with the current parent repo path.
- Effect: a parent repo at `children/a` would recurse into `grands/g` instead of
  `children/a/grands/g`, so deep child transactions were skipped during
  recovery.

### Bug 2. Coordinated child transactions can be re-entered after root finish

- Symptom: a retained child `TransactionContext` object can be entered again
  after the outer/root transaction has already finished.
- Root cause: only the root transaction was marked finished on successful
  completion; descendants were unbound from their repos but not marked finished.
- Effect: stale child transaction objects remained re-enterable even though the
  coordinated logical transaction tree was already over.

### Bug 3. Tree cleanup stops on the first child cleanup failure

- Symptom: rollback or detach-for-recovery of a coordinated transaction tree can
  stop early if one child cleanup step raises.
- Root cause: the cleanup walkers recursed directly into each child without
  isolating failures, so a child exception prevented later sibling and parent
  cleanup.
- Effect: locks/resources could remain held on the parent or on later siblings,
  and cleanup could leave the tree only partially torn down.

### Fix direction

- Join relative child repo paths to the current repo path during SQLite nested
  recovery.
- Mark the whole transaction tree finished whenever the root finishes or enters
  the recover-only state after commit finalization failure.
- Make rollback and detach-for-recovery best-effort across the whole tree:
  continue cleaning siblings and parent resources, then re-raise the first
  cleanup failure.
