# LMDBCache

## ⚠️ Important Warning

**LMDBCache is problematic with fork-based multiprocessing!** 

Before using LMDBCache, please consider using [`joblib.Memory`](https://joblib.readthedocs.io/en/latest/memory.html) instead. `joblib.Memory` provides similar caching functionality but is more user-friendly and works reliably with multiprocessing scenarios. It offers:

- Better multiprocessing support
- Automatic cache invalidation
- More intuitive API

**Use LMDBCache only if you have specific requirements that `joblib.Memory` cannot fulfill.**

## What is LMDB?

LMDB (Lightning Memory-Mapped Database) is a high-performance, memory-mapped key-value store. It provides:

- **Fast read/write operations**: Memory-mapped files for optimal performance
- **ACID compliance**: Reliable transactions with consistency guarantees  
- **Compact storage**: Efficient data organization with minimal overhead
- **Cross-platform**: Works on Linux, macOS, and Windows

LMDBCache wraps LMDB to provide a simple Python caching interface with automatic serialization using pickle.

## Key Points to Use it Correctly

It is stated clearly in the [lmdb document](http://www.lmdb.tech/doc/) that:

> Restrictions/caveats:
> 
> - Use an MDB_env* in the process which opened it, without fork()ing.

This means that **a LMDB connection (or environment, as it is called in LMDB) should not be shared across processes.** 

However, the default method of Linux is fork, which shares the memory space of the parent process, and thus shares the LMDB environment. So, a LMDBCache instance **should not be created before any possible fork**, and should be created in each process separately.

The correct way to use LMDBCache:

- Pass cache configuration to each process
- Create the process-local LMDBCache instance in each process

Note: if you use `spawn`, the LMDB environment will not be shared. 

The folowing sections are examples to demonstrate both correct and incorrect usage patterns.

## ✅ Correct Usage

This example shows how to properly use LMDBCache with multiprocessing by creating separate cache instances within each worker process, avoiding shared state issues.

``` py title="correct_usage.py"
--8<-- "docs/guides/_lmdb_examples/correct_usage.py"
```


## ❌ Incorrect Usage

### Deliberately Closing a Shared LMDB Environment

This example demonstrates the problems that arise when deliberately closing a shared LMDB environment in a child process, leading to `lmdb.BadRslotError`.

``` py title="incorrect_usage_case_1.py"
--8<-- "docs/guides/_lmdb_examples/incorrect_usage_case_1.py"
```

The origin of the problem is that when the child process closes the shared LMDB environment, it invalidates the file descriptor for the parent process as well, while the parent process is still trying to use it.

### Unintentionally Closing a Shared LMDB Environment

Even when we take special care to avoid touching the shared LMDB environment, it can still be unintentionally closed through garbage collection in a child process, breaking the cache in the parent process.

``` py title="incorrect_usage_case_2.py"
--8<-- "docs/guides/_lmdb_examples/incorrect_usage_case_2.py"
```

The problem arises from the fact that [py-lmdb](https://github.com/jnwatson/py-lmdb) closes the LMDB environment in the `__del__` (see [`lmdb.Environment.__del__`](https://github.com/jnwatson/py-lmdb/blob/master/lmdb/cffi.py#L1410)). As we cannot modify the `py-lmdb` library, we cannot prevent this behavior.
