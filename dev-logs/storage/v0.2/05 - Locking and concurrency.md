# Locking and concurrency

This document refines the concurrency rule for `storage v0.2`.

The transaction model in `02 - Txn & nested txn.md` intentionally avoids
multi-writer conflict resolution. That does not mean the implementation must
always serialize the whole repo tree behind one exclusive lock. The repo tree
already gives the system a natural hierarchy of ownership domains. The locking
design should use that hierarchy directly.

The goal is simple:

- at most one independent writer may own one repo subtree
- independent writers for disjoint sibling repo subtrees may overlap
- a root transaction may still coordinate nested child transactions beneath it
- lock acquisition must stay simple enough to avoid deadlock

## 1. Repo locks and intention locks

Each repo in a persisted repo tree has a lockable ownership domain. A write
transaction can hold two kinds of locks on that domain:

```text
X  = exclusive write lock on this repo subtree
IX = intention-to-write lock on an ancestor repo
```

An exclusive lock means the transaction owns that repo subtree for writing. No
other independent writer may write that repo or any repo below it while the
exclusive lock is held.

An intention lock means the transaction does not own the ancestor subtree
exclusively, but it is writing somewhere below that ancestor. The intention lock
is the live-process signpost that prevents another transaction from mistaking the
ancestor subtree for clear.

The essential compatibility rule is:

```text
IX is compatible with IX
X conflicts with X
X conflicts with IX
```

This is what allows sibling writers to coexist while still preventing a wider
ancestor writer from starting over them.

## 2. Shrinking the exclusive domain

An independent transaction enters the tree from the persisted tree root. It may
then shrink its exclusive domain top-down until it reaches the repo it wants to
write.

The important detail is that only the exclusive domain shrinks. The transaction
does not release all evidence from the ancestors. It retains intention locks on
those ancestors until the transaction commits, rolls back, or recovers.

For a direct write to a nested repo, the lock shape is:

```text
tree root IX -> ... -> parent IX -> target repo X
```

This says: the transaction owns the target repo subtree for writing, and the
ancestors remember that some descendant writer exists.

The design prohibits lock upgrading. A transaction may acquire locks only while
moving downward through the repo tree. It may not start at a child and later try
to turn an ancestor `IX` into `X`. If an operation needs a wider ownership
domain, it must start a new transaction at that wider repo after the current
transaction has ended.

This top-down, no-upgrade rule is the main deadlock defense. All writers acquire
locks in the same tree order, and none of them waits while trying to climb back
up the tree.

## 3. Tree-root transactions

If an application opens a transaction directly on the tree root repo, the lock
shape is:

```text
A(X)
```

This excludes every independent direct-open writer below `A`. A direct-open
transaction for a child repo would need an intention lock on `A`, and `A(IX)`
conflicts with `A(X)`.

This does not prevent nested transactions coordinated by the root transaction.
Those nested transactions are not independent writers. They are participants
under the same root transaction authority.

So a root transaction with:

```text
A(X)
```

may still coordinate child transactions for `B`, `C`, and `D` inside the same
transaction tree. The root transaction owns the external write authority; the
children express closed nested transaction structure under that authority.

## 4. Direct child transactions

Consider this repo tree:

```text
A
  B
    C
  D
```

If a process directly opens repo `C` for writing, the lock shape is:

```text
A(IX) -> B(IX) -> C(X)
```

This means `C` is the root of this independent transaction. `A` and `B` are not
transaction parents. They are only lock ancestors that prevent wider conflicting
writes while `C` is active.

Another process may still directly open `D` for writing:

```text
A(IX) -> D(X)
```

The two transactions are compatible because both hold only `IX` on `A`, and
their exclusive locks are on disjoint repo subtrees.

However, while either transaction is active, a new independent transaction on
`A` cannot begin:

```text
A(X)
```

That would conflict with the existing `A(IX)` lock.

## 5. Lock authority versus transaction authority

The lock tree and the transaction participant tree are related, but they are
not the same thing.

The lock tree protects external concurrency. It answers the question: which
repo subtree may this independent writer mutate without conflicting with other
independent writers?

The transaction participant tree records closed nested coordination. It answers
the question: which repos are participating in the same root transaction, and
which root transaction has final commit or rollback authority?

For a direct transaction on `C`:

```text
A(IX) -> B(IX) -> C(X)
```

`C` is the transaction root. `A` and `B` do not receive child prepared records,
do not authorize `C` commit, and do not become transaction participants. They
only retain intention locks.

For a transaction opened on `A`:

```text
A(X)
```

`A` is the transaction root. If the application opens child transactions for
`B`, `C`, or `D` through that root transaction, those children become
subordinated participants. Their child commits reach `prepared`, and only `A`
may authorize final publication of the coordinated tree.

This distinction preserves the repo ownership rule from the transaction model:
a parent repo never directly modifies child repo content. Parent/root authority
is control-plane authority, not byte ownership.

## 6. Persisted tree discovery

The hierarchical lock protocol requires a repo opened directly as a child to
know its persisted tree root and its ancestor path. Otherwise the child could
mistakenly lock itself as an independent tree root and miss a wider transaction
already active above it.

For that reason, each repo should persist enough tree metadata near its blob
state to discover:

- the persisted tree root relative to this repo
- this repo's path from that tree root
- enough repo identity to reject stale or mismatched metadata

The concrete metadata file and exact payload are implementation details for a
later document. The semantic requirement is already fixed: before an
independent write transaction begins, the repo must discover the canonical tree
root, acquire locks top-down from that root, and refuse to proceed if the
metadata is missing, stale, or contradictory in a way that could create two
authorities.

There is one bootstrap limitation. Opening an empty filesystem path with no
repo-tree metadata initializes that path as a new tree root. If the path is
intended to be a nested repo, it must first be materialized through the parent
schema so the child metadata records the correct tree root and ancestor path.
Directly opening an unmaterialized nested repo cannot safely infer that parent
relationship without guessing from the filesystem layout, so the design treats
it as a new independent tree.

## 7. Recovery and lock lifetime

Write locks are held until the transaction reaches a terminal state:

```text
commit -> clear
rollback -> clear
recovery -> clear
```

For app-crash consistency, recovery must treat lock/fence/journal state as part
of the same defensive story. A crashed transaction may release its live OS locks
while still leaving fences, WAL records, staged files, or stale lock artifacts
behind. A later opener must not ignore those artifacts and read or write as if
the repo were clear.

This does not require the lock file itself to be the transaction authority. The
journal remains the lifecycle authority, and the blob-side fence remains the
split-brain witness. Locks are the concurrency gate used while processes are
alive. After accidental process exit, the persistent journal and fence decide
whether recovery is required.

The intended rule is therefore:

```text
live process concurrency: protected by hierarchical locks
post-crash safety: protected by journal replay and blob-side fences
```

Keeping those responsibilities separate prevents the lock design from becoming
a second transaction protocol.

## 8. Realization point across blob backends

The primary live lock must be realized by the journal backend, not by the blob
backend.

This follows from the backend split in `03 - Backend architecture.md`. Blob
backends may be heterogeneous inside the larger storage system. A filesystem
blob backend, an OSS blob backend, and a later backend family do not offer the
same native concurrency primitives. Filesystem locks, database advisory locks,
NFS behavior, and object-store conditional writes have different guarantees and
failure modes. If the core design required every blob backend to provide the
same live lock semantics, the abstraction would either become false or collapse
back into the weakest backend.

The intended division is therefore:

```text
blob backend    = committed/staged bytes, repo-local fence, tree discovery
journal backend = transaction truth, recovery authority, live lock manager
```

The lock keys should be semantic repo-tree keys rather than raw physical blob
paths:

```text
tree_id   = stable identity of the persisted repo tree
repo_path = path from the tree root to this repo
lock key  = (tree_id, repo_path)
```

The blob backend stores enough metadata and fence information to discover and
validate those keys. The journal backend decides how to realize the actual
`IX` and `X` locks for those keys.

For a filesystem-backed JSONL journal, the implementation may realize locks as
repo-tree lock files or as a compact lock table guarded by a short-lived mutex.
For a SQL journal backend, the implementation may realize the same semantic
locks as rows in a lock table or as database advisory locks. In both cases, the
logical protocol is the same even though the physical locking mechanism differs.

An OSS blob backend should not be treated as the primary lock provider. It
should persist the repo's fence and tree-discovery metadata near the
authoritative objects, then rely on the configured journal backend for live
concurrency. If multiple machines may write the same OSS-backed repo tree, the
journal backend must itself be shared by those machines. A host-local JSONL
journal can only provide host-local live locking.

The begin sequence for an independent child transaction is therefore:

```text
1. read blob-side tree metadata and fence from the target repo
2. discover the canonical tree root and repo path
3. verify that the configured journal backend matches the blob-side fence
4. acquire hierarchical locks from the journal backend
5. begin the transaction through that same journal authority
```

This keeps heterogeneous blob backends honest. Blob storage remains the data
plane and discovery witness. The journal backend remains the single live
authority for transaction state, recovery, and locking.
