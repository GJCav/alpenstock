# Rev 2 Coordination Bug Record

Date: 2026-04-10

This note records the coordination and lifecycle bugs found after the Rev 2 implementation landed.

These are not theoretical concerns. They were confirmed by code review and direct local repro.

## 1. Deep nested repo enrollment is flattened incorrectly

Observed problem:

- when a grandchild repo is first written under an active root transaction
- the current implementation enrolls that grandchild directly under the nearest active ancestor transaction
- it does not first enroll the intermediate parent repo as a child participant

Why this is wrong:

- the historical design chooses a true parent/child coordinator tree
- parent WAL / metadata should reflect the actual nested repo structure
- recovery is supposed to walk the enrolled repo tree, not a flattened set of arbitrary descendants

Required fix:

- enrolling a nested repo must preserve the actual repo hierarchy
- if an intermediate parent repo is not yet enrolled, it must be enrolled first
- only then may the deeper child repo be enrolled under that parent repo transaction

## 2. Coordinated child transaction re-entry is rejected

Observed problem:

- inside an active outer transaction
- opening `child_repo.transaction()` once creates a coordinated child transaction
- opening `child_repo.transaction()` again during the same outer transaction currently raises the same-repo nested prohibition

Why this is wrong:

- same-repo nested prohibition applies to creating a second local transaction on the same repo
- it should not block re-entering the already-enrolled coordinated child participant
- the intended behavior is “join the same child logical transaction”

Required fix:

- if the repo already has an active coordinated child transaction
- `repo.transaction()` should return that existing child participant
- entering it again should reuse the same logical child transaction

## 3. Post-commit-failure lifecycle is unsafe and confusing

Observed problem:

- commit order is deepest-first
- so a failure during root finalization may happen after one or more descendants have already committed
- after such a failure, `rollback()` is still exposed on the root transaction object

Why this is wrong:

- at that point the logical transaction may already be partially published
- rollback is no longer a valid semantic operation
- the only correct next step is recovery / finish-commit

Additional practical issue:

- if the transaction object keeps repo locks or backend resources after finalization failure
- external recovery cannot run cleanly in the same process

Required fix:

- after commit finalization failure, rollback must be rejected
- backend resources and locks should be released into a recoverable state
- the transaction object should clearly transition into “recovery required”

## 4. Fix scope

The follow-up fix pass should cover:

1. hierarchy-preserving nested enrollment
2. coordinated child transaction re-entry
3. finalization-failure lifecycle and resource release
4. regression tests for all three cases
