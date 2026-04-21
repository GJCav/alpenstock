# Txn & nested txn

This document defines the semantic core of transactions in `storage v0.2`.

It fixes four decisions that shape the rest of the design:

- transactions use copy-on-write staging
- the isolation level is read-committed
- write concurrency is controlled by hierarchical repo-tree locking
- nested transactions follow classic closed nested transaction semantics

## 1. Repo tree and ownership

`Repo` is the boundary for ownership.

Each repo owns only the logical files that belong directly to its own schema
domain. A nested child repo is not merely a directory inside its parent. It is
a separate transaction domain with its own ownership boundary. From this it
follows that a parent repo never directly modifies the files of a child repo,
and sibling repos never directly modify each other's files either.

The shape of transactional coordination is therefore a tree, not a flat key
space:

```text
root repo txn
  child repo txn A
    grandchild repo txn A1
  child repo txn B
```

Under this tree structure, the parent is not a wider namespace that can directly
"see into" child repo contents. Instead, the parent is the enclosing transaction
context that coordinates commit and rollback across child transaction domains.

Sibling child transactions may overlap in time under the same root transaction.
That is safe because their owned file domains are disjoint by construction.

## 2. Committed state, staged state, and visibility

The design uses copy-on-write semantics. Committed data is never edited in
place. A write-capable open works on staged data derived from the transaction's
current visible state, and committed data remains unchanged until the root
transaction publishes a final result.

This leads to three notions that must be kept distinct.

Committed state is the durable state visible outside active write transactions.
Staged state is the transaction-local candidate data produced by copy-on-write
editing. Transaction-visible state is the logical state seen by code that is
executing inside an active transaction.

The isolation level is read-committed:

- read-only open outside a transaction sees committed state
- read inside a transaction sees transaction-visible state

## 3. Mathematical model of one transaction

Within one repo domain, let:

```text
K = logical keys
V = full logical values
⊥ = absence
```

Committed state is modeled as:

```text
S : K -> V ∪ {⊥}
```

A transaction is modeled as:

```text
Tx = (S_base, Δ, state)
```

where:

- `S_base` is the base state visible when the transaction begins
- `Δ` is the transaction overlay
- `state` is the lifecycle state

The overlay is:

```text
Δ : K -> Put(V) | Delete
```

and transaction-visible lookup is:

```text
SΔ(k) =
  Δ(k).value          if Δ(k) = Put(value)
  ⊥                   if Δ(k) = Delete
  S_base(k)           if k not in dom(Δ)
```

This is the formal version of "read inside a transaction sees
transaction-visible state". A successful write does not mutate committed state.
Instead it changes `Δ`, and future reads in that same transaction observe the
result through `SΔ`.

Overlay application is written:

```text
S1 = S0 ⊕ Δ
```

where `Put(v)` replaces the value for that key, `Delete` removes it, and
untouched keys remain unchanged.

One small example is enough to make the notation concrete. Suppose committed
state is:

```text
S0(config) = "version = 1"
S0(notes)  = "hello"
```

and the transaction overlay becomes:

```text
Δ(config) = Put("version = 2")
Δ(notes)  = Delete
```

Then the transaction-visible state is:

```text
SΔ(config) = "version = 2"
SΔ(notes)  = ⊥
```

and a successful commit would publish:

```text
S1 = S0 ⊕ Δ
```

which means `config` is replaced and `notes` is removed.

## 4. File semantics under copy-on-write

The mathematical model above becomes a file API through copy-on-write staging.
The rule is simple: writable opens never edit committed files directly.

A writable handle operates on staged data. On successful close, the final value
produced by that handle becomes the transaction's candidate value for the
corresponding logical key:

```text
close_writer(k, v_final) => Δ[k] := Put(v_final)
delete(k)                => Δ[k] := Delete
```

This leads to a characteristic behavior that the implementation must preserve.
If a key is reopened for writing inside the same transaction, the new writable
session must begin from the current transaction-visible value, not from old
committed state. In other words, reopening continues from the transaction's
latest logical result.

If a writable handle fails before successful close, the module is not required
to preserve its partial writes. What matters is the stronger invariant:
committed durable state remains untouched.

The exact physical form of the staged data is left unspecified here. Later
documents may choose one concrete representation, but the semantics already fix
what that representation must mean.

## 5. Lifecycle as a semantic progression

The public lifecycle states are:

- `open`
- `prepared`
- `committed`
- `aborted`

These states are not merely labels. They describe four qualitatively different
points in the life of a transaction.

An `open` transaction is still under construction. Writable handles may still
be open, the overlay may still change, and there has been no publication.

A `prepared` transaction is frozen intent. All writable handles are closed, the
overlay can no longer change, and enough recovery metadata must exist for the
backend's crash-consistency target.

A `committed` transaction is one whose semantic result has taken effect. The
meaning of that sentence becomes subtle in the nested case. Under outer
coordination, a child may be finalized only to stable `prepared` state while
the root transaction remains open. The transition to final `committed` state is
authorized by the root for the coordinated tree as a whole.

An `aborted` transaction is one whose pending state has been discarded.

The key point is that `prepared` is the point at which a transaction becomes a
stable semantic object for recovery, while `committed` is the point at which
its result has actually taken final effect. For the coordinated tree, that
final effect is authorized only at the root.

## 6. Single-writer discipline

The model is intentionally built around a single-writer discipline.

At the repo level, there may be at most one active independent writer for the
same repo ownership domain at a time. This is the design's main simplification.
It removes the need for multi-writer conflict resolution and keeps the recovery
story small enough to be explained precisely.

The tree structure allows this rule to be enforced without always taking one
exclusive lock over the whole tree. A later concurrency note defines the exact
hierarchical locking discipline: independent writers acquire locks top-down,
retain intention locks on ancestors, and take an exclusive lock only over the
repo subtree they actually write.

Within one transaction, there may also be at most one active writable handle
for the same logical key at a time. The reason is similar. Multiple open
writable handles for the same key would force the design to define ambiguous
merge rules for overlapping edits to one logical file, which this module does
not want to do.

## 7. Closed nested transactions

With the state model in place, nested transaction coordination can now be
stated precisely.

The essential rule of the design is:

```text
subordinated child commit = child reaches prepared under root coordination
root commit               = final durable publication of the coordinated tree
```

That is what makes the model a classic closed nested transaction system rather
than a coordination scheme among independently publishing children.

### 7.1 Child begin

When a child repo begins a transaction under an active ancestor transaction, it
does not become an independent durable publisher. It becomes a nested
participant in the same root logical transaction.

Formally, the child receives its own transaction:

```text
Tx_child = (S_child_base, Δ_child, state_child)
```

where `S_child_base` is derived from the enclosing parent-visible state for that
child repo domain at child-begin time. From that point on, the child maintains
its own child-local overlay while open.

### 7.2 Child commit under outer coordination

Suppose a child reaches prepared state with overlay `Δ_child`. Then child
commit under an outer transaction is not an independent transition to final
durable `committed` state.

Instead, it means the child has completed local mutation, closed its writable
handles, satisfied its local consistency checks, and reached stable
`prepared` state under the authority of the enclosing root transaction.

Formally, the child has a well-defined prepared logical state:

```text
S_child_result = S_child_base ⊕ Δ_child
```

but that state is still subordinated to root coordination. No external observer
outside the root transaction may treat the child as finally committed.

This is the heart of closed nesting in the repo tree. Child success is real,
but it is not independently final.

### 7.3 Child abort

Child abort discards child-local pending state only.

The child transaction disappears without changing committed durable state and
without forcing the parent to lose its own pending work. This is exactly the
classic closed nested behavior: child failure is local unless the application
chooses to escalate it.

If a child has already reached `prepared` under outer coordination and the root
later aborts, that prepared child also ends in `aborted` state as part of the
aborted coordinated tree.

### 7.4 Root commit

Only the root transaction may publish new committed durable state.

Let the final root-visible overlay be `Δ_root_final`. Then root commit is the
unique step that performs:

```text
S1 = S0 ⊕ Δ_root_final
```

and thereby changes externally visible committed state.

At that point, the whole coordinated prepared tree becomes `committed`. No
subordinated child may become finally `committed` before this root-authorized
transition occurs.

This one distinction separates the `v0.2` design sharply from the old `v0.1`
coordination model. In `v0.1`, a child repo could publish before the parent
finished. In `v0.2`, child commit is not publication at all.

## 8. A transaction trace through a repo tree

The semantics become clearer when read as an execution trace.

Suppose a root repo `WorkspaceRepo` contains nested child repos `users/alice`
and `users/bob`.

First, the root transaction begins. Its base state is committed state for the
root repo domain.

Then `users/alice` begins a child transaction beneath that root. The child base
state is whatever the root transaction makes visible for the `users/alice` repo
domain at that moment.

Alice's child transaction writes `profile.toml` and then commits. This does not
publish `users/alice/profile.toml` as new durable state. Under outer
coordination, Alice's child transaction instead reaches stable `prepared` state
and remains under the authority of the root transaction.

Meanwhile `users/bob` may also have its own overlapping child transaction. Bob
owns a different child repo domain, so there is no conflict merely because both
child transactions are open at the same time.

Finally, when the root transaction commits, the whole accumulated root-visible
result is published durably. Only at that point do Alice's and Bob's changes,
and the coordinated child transactions themselves, become finally `committed`.

This is the intended reading of closed nested transactions in the repo tree:
children may finish logically before the root, but they do not publish before
the root.

## 9. Prepare and commit over the tree

The tree structure gives a natural order to prepare and commit.

Prepare is deepest-first. A child must freeze before its parent can finish
preparing, because the parent's prepared state must already include the stable
semantic result of every prepared descendant.

Commit is also tree-structured, but under closed nesting its meaning is
different from the `v0.1` design. Subordinated children do not independently
cross into final durable `committed` state before the root does. Instead, they
remain prepared under root coordination until final root commit.

So the sequence is:

1. deepest children prepare
2. intermediate parents prepare
3. root prepares
4. subordinated prepared children remain under root coordination
5. root commits
6. the coordinated prepared tree becomes committed and is published durably

The final step alone crosses the boundary between transaction-visible staged
state and externally visible committed state.

## 10. Recovery as a root-level correctness claim

Because only the root may publish durable state, recovery correctness is also a
root-level claim.

For one root transaction, recovery must end in a state observationally
equivalent to one of:

- rollback to the old committed state
- full commit of the final root-visible result

It must never leave a stable mixed outcome where one committed observer can see
only part of the root transaction's result.

The nested case sharpens this requirement. A child may have reached stable
`prepared` state under root coordination, but if the root has not yet committed,
recovery must not treat that prepared child as final externally visible durable
state. Child-prepared-but-root-uncommitted is not a valid final externally
visible state.

This is why later recovery and journal documents must be root-driven. The exact
mechanism is left for later, but the semantic obligation is already fixed here.

## 11. Required invariants

The design can be summarized as a set of invariants that any implementation
must preserve.

- Committed durable data is never modified in place by open writers.
- Read-only open outside a transaction sees committed state.
- Read inside a transaction sees transaction-visible state.
- At most one active write transaction exists for the repo tree.
- At most one writable handle per key exists within one transaction.
- A parent repo never directly modifies child repo files.
- Under outer coordination, child commit finalizes the child at `prepared`, not
  at final durable `committed`.
- Child commit is not durable external publication.
- Child abort discards only child-local pending state.
- Only root commit changes externally visible committed state and transitions
  the coordinated prepared tree to `committed`.
- Root commit is equivalent to exactly one application of the final root-visible
  overlay.
- Recovery never leaves a mixed partial externally visible result as the final
  outcome.

## 12. Consequences for later documents

This semantic model constrains all later design work.

The journal design must be able to represent prepared root state without
confusing subordinated child preparation with external publication. The
recovery design must be root-driven. The staging design must preserve
copy-on-write semantics without leaking staged state as committed state. And
any future split between blob storage and journal storage must continue to
respect the single semantic boundary that matters most here: only the root
transaction may publish.

This document therefore serves as the formal center of the `v0.2` redesign.
