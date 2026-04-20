## Rev 2 Lifecycle Failure Attribution Bugs

This note records the follow-up bugs found in transaction coordination around
failure attribution and cleanup order.

### Bug 1. Commit cleanup failure masks the real commit failure

- Symptom: if commit finalization fails and detach-for-recovery also fails, the
  detach error is raised instead of the original commit error.
- Impact: the caller sees the wrong primary failure, and the transaction tree
  may not be marked fully finished before the detach error escapes.
- Desired behavior: preserve the original commit failure as the primary error,
  attach cleanup failures as notes, and always mark the whole coordinated tree
  finished before leaving the failing path.

### Bug 2. Abort cleanup failure masks the real transaction failure

- Symptom: if a transaction body raises or prepare fails, and rollback/abort
  cleanup also fails, the cleanup error replaces the original failure.
- Impact: user errors and prepare failures are hidden by cleanup failures, which
  makes debugging much harder and distorts the real lifecycle result.
- Desired behavior: preserve the original body/prepare failure as the primary
  error, and attach cleanup failures as notes instead of replacing it.

### Fix direction

- In `TransactionContext.__exit__`, preserve the primary failure from:
  - transaction body exceptions
  - prepare failures
  - commit failures
- Run cleanup best-effort and attach cleanup failures as exception notes.
- Ensure commit-failure cleanup marks the whole coordinated tree finished even if
  detach-for-recovery itself raises.
