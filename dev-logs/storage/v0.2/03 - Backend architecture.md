# Backend architecture

This document defines the backend architecture of `storage v0.2`.

The previous document fixed the semantic contract of transactions, nested
transactions, and root-coordinated commit. The purpose of the present document
is to explain how that semantic contract can be realized across different
storage media without collapsing the design back into a filesystem-only model.

The document fixes the high-level architectural split and the compatibility
rules between backend families. It does not yet define exact journal schemas,
exact recovery algorithms, exact fence payloads, or low-level publication
mechanics. Those are intentionally deferred to later design documents.

## 1. Architectural thesis

In `v0.2`, a repo is not merely a schema projected onto bytes. It is a semantic
unit realized by a triplet:

```text
(schema, blob backend, journal backend)
```

The schema defines the logical structure of files and nested repos. The blob
backend realizes the committed and staged data of that repo. The journal
backend realizes transaction authority, lifecycle state, coordination, and
recovery.

This decomposition becomes necessary because the module now aims to support more
than one class of storage medium. A local filesystem can naturally express
copy-on-write publication through rename-based techniques. An object store
cannot. Yet both must satisfy the same transaction semantics. The only way to
keep the semantic contract stable across such different media is to separate
the data plane from the authority plane.

The blob backend therefore becomes the place where a repo's bytes live and are
published in a backend-native way. The journal backend becomes the place where a
repo's transaction truth lives.

## 2. Why blob and journal must be separated

The original filesystem-centric design implicitly coupled staged data,
publication mechanics, and transaction authority into one filesystem-local
implementation. That coupling works as long as the committed namespace is a tree
of ordinary files and publication can be expressed as rename or replace.

Once this assumption is relaxed, the coupling becomes a liability. An OSS
backend cannot provide a POSIX-style rename boundary. It therefore cannot be
forced into a design where transaction coordination is inferred directly from
filesystem-like publication events. Conversely, the transaction semantics from
`02 - Txn & nested txn.md` must remain unchanged whether the repo stores its
data on local files or on object storage.

The separation between blob and journal exists to resolve this tension. The
blob backend is responsible for repo-local byte storage and repo-local
publication mechanics. The journal backend is responsible for the transaction
lifecycle, single-writer discipline, nested coordination, root commit
authority, and recovery authority.

This division is not merely an implementation detail. It is the architectural
condition that makes backend extensibility compatible with one stable
transaction model.

## 3. The blob backend

The blob backend is the backend responsible for the data plane of one repo. It
owns the repo's committed blob namespace, whatever staged or publishable blob
representation that backend requires, and the repo-local operation that turns a
prepared state into committed state.

The blob backend is not the authority for transaction truth. It does not decide
which transaction is the real root authority, whether a nested coordinated tree
is allowed to commit, or how recovery chooses between rollback and final
publication. Those are journal questions. The blob backend instead answers a
different question: once a transaction has reached a publishable state under the
journal's authority, how are the bytes for this repo actually materialized as
committed state?

Two blob backend families are planned in `v0.2`.

The first is the filesystem blob backend. In that family, the committed
namespace is represented directly as ordinary files, and copy-on-write staging
can be implemented using filesystem-local artifacts and rename-based
publication. This family naturally supports what the introduction document
called user transparency: committed data is directly visible as ordinary files.

The second is the OSS blob backend, meaning S3-compatible object storage. In
that family, the committed namespace cannot be treated as a live local file tree
with rename-based publication. The backend must therefore use a different
publication method, most naturally a manifest-and-publish style model. This
family does not offer native user transparency in the local-filesystem sense.
Instead it offers a weaker but still intentional form: projectable user
transparency. The committed repo remains file-shaped and can be surfaced to
users through a local checkout or sync workspace, even though the authoritative
storage is remote.

This distinction between native and projectable transparency is not a cosmetic
difference. It shapes the implementation choices that are reasonable for each
blob backend family.

## 4. The journal backend

The journal backend is the authority plane of the system. It is responsible for
transaction lifecycle state, single-writer coordination, root authority over a
coordinated transaction tree, and recovery authority after accidental process
exit.

Calling it a "journal backend" is useful because it reminds us that transaction
state must be durably recorded somewhere. But the journal backend is more than a
log sink. It is the source of truth for questions such as:

- whether a repo is clear, active, subordinated, or recovery-required
- whether a child repo is under root coordination
- whether a prepared tree may be committed or must be aborted
- which root transaction has authority over final publication

Two journal backend families are planned in `v0.2`.

The first is filesystem-backed append-only JSONL WAL. The second is
SQL-compatible journal storage, such as a SQLite database or a remote SQL
endpoint such as PostgreSQL. The internal architecture of these journal
backends is intentionally deferred. At this stage the important point is not the
exact record shape, table schema, or lock algorithm, but the architectural role
they play.

The journal backend is therefore defined here by responsibility rather than by
storage format.

## 5. The journal uniformity rule

The separation between blob and journal does not mean complete independence
between all backend choices. In particular, the semantics of nested
coordination place a strong restriction on journal selection.

Different blob backends can coexist under one coordinated transaction tree
because each repo owns only its own bytes. Blob publication remains repo-local,
even when the root transaction coordinates a larger tree.

Journal backends are different. The journal backend is not merely storing data;
it is the authority over lifecycle, root coordination, and recovery. If two
repos in one coordinated tree were allowed to use different journal backends,
the system would no longer have one authority over prepare, commit, abort, and
recovery. It would instead need cross-journal distributed coordination. That is
far outside the intended complexity of `v0.2`.

For this reason, `v0.2` adopts the journal uniformity rule:

- independent repos may use different journal backends
- but one coordinated transaction tree must use one journal backend authority

This rule keeps nested coordination coherent without preventing backend
extensibility in the independent-repo case.

The same rule implies a policy on rebinding. A repo may switch from one journal
backend type to another only when its transaction state is clear. If the repo
or any coordinated tree above it is active, prepared, subordinated, or
recovery-required, rebinding must be refused.

This is not an arbitrary operational caution. It is part of preserving one
unambiguous authority over the repo's transactional state.

## 6. Coordination discoverability and split-brain prevention

Once a repo may be subordinated under an outer transaction, the system must be
able to discover that fact before it begins a new independent transaction on
the repo. Otherwise a child repo could be opened independently while already
under root coordination, or a crash followed by bad reconfiguration could cause
the application to create a second transaction authority over the same repo.

At first glance, one might think the journal backend alone should be enough to
solve this problem. In the normal case, it is indeed the authority. But the
design must also defend against misconfiguration. A user may crash the app,
change the configured journal backend, change temporary workspace paths, and
then directly open a child repo as if it were clear. In that extreme case, a
pure journal-only check is not enough, because the application may be asking the
wrong journal backend.

For this reason, `v0.2` introduces a second line of defense: a per-repo fence
stored in the blob backend itself.

This fence is not the transaction authority. The journal backend remains the
authority. The fence exists instead as a defensive witness stored with the repo
and blob backend. Its purpose is to make coordination state discoverable close
to the repo's actual data, and to prevent split-brain after crash or bad
reconfiguration.

The fence is uniform across blob backends. A filesystem repo stores the fence in
its repo control area. An OSS repo stores the fence near its repo control
prefix in object storage. In both cases, the fence moves with the repo's blob
storage, not with a configurable temporary-file path.

The blob-side fence must therefore make at least these facts discoverable:

- the identity of the repo
- the identity and type of the journal backend currently bound to it
- whether the repo is clear or not clear
- whether it is subordinated under an outer coordinated transaction
- whether recovery is still required

The exact serialized fields are deferred to later documents, but the
architectural requirement is already fixed: before begin, recover, or rebind,
the storage layer must consult both the journal backend and the blob-side fence.
If either side indicates unsafe state, or if they disagree about authority, the
operation must be refused.

This is the central split-brain defense of the architecture. The journal
backend decides the transaction truth. The blob-side fence prevents unsafe
creation of a second authority when the configured journal truth is wrong,
missing, or mismatched.

## 7. Backend family narratives

The abstract split becomes easier to understand when written as concrete backend
families.

In the filesystem case, the architecture is comparatively direct. The blob
backend stores committed files directly under the repo root and can stage data
in a repo-local transaction area. Publication can be expressed through
filesystem-native copy-on-write and rename-style operations. The repo-side fence
can live naturally beside the repo's transaction control area. This blob backend
works naturally with a local JSONL WAL journal backend, and it can also work
with a SQL journal backend if the application prefers centralized transaction
authority.

In the OSS case, the architecture is more indirect but still coherent. The blob
backend stores committed state as remote objects, most plausibly through a
manifest-and-publish model rather than rename-style publication. User-facing
transparency is achieved not by direct ordinary files in the authoritative
storage, but by projecting the committed repo into a local checkout or sync
workspace. The repo still needs a blob-side fence near its authoritative blob
storage, because that is the only reliable place to keep a durable anti-split-
brain witness that survives local temp-path changes and host-specific scratch
state.

The OSS blob backend is expected to work especially well with SQL journal
backends, because SQL naturally centralizes transaction authority and root
coordination. An OSS blob backend paired with a filesystem-backed JSONL journal
is not impossible, but it is a more specialized deployment shape and should not
be treated as the default architecture.

The important point across both families is that blob publication remains
repo-local, while transaction truth remains journal-controlled.

## 8. Deferred implementation questions

This document deliberately stops before several implementation-level decisions.

Most importantly, it does not yet define:

- the exact internal architecture of JSONL journal backends
- the exact schema of SQL journal backends
- the exact writer lease mechanism
- the exact serialized format of the blob-side fence
- the exact publication algorithm for filesystem blob backends
- the exact manifest-and-publish algorithm for OSS blob backends
- the exact garbage-collection strategy for stale staged blobs
- the exact rebinding procedure when switching journal backends from a clear
  state

These omissions are intentional. The role of the present document is to fix the
architectural constraints within which those later implementation decisions must
fit.

## 9. Design consequences

Three consequences follow from this architecture.

First, repo ownership remains strict. A repo owns its own logical files, its own
blob backend presence, and its own fence. Nested coordination does not erase
repo boundaries; it coordinates them.

Second, transaction semantics remain uniform even when blob backends vary. The
root-coordinated semantics from `02 - Txn & nested txn.md` do not depend on
whether one repo publishes through local rename-based techniques and another
through remote manifest publication.

Third, the design is extensible without becoming authority-fragmented. Blob
backends may vary per repo. Journal backends may vary only across independent
repos. A coordinated tree has one journal authority, and every repo carries a
blob-side fence that prevents silent split-brain when authority is misbound.

This is the architectural center of `v0.2`: one transaction model, multiple
blob families, one journal authority per coordinated tree, and a uniform
blob-side fence stored with every repo.
