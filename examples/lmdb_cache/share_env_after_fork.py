"""
Example of incorrect usage of LMDBCache in a multiprocessing context.

It is documented by the `lmdb` library that the LMDB environment should not be
shared after a fork. So the combination of `lmdb` and `fork` is dangerous.

This example demonstrates two common failure cases:

1. Deliberately closing the shared LMDB environment in a child process

2. Unintentionally closing the shared LMDB environment in a child process

Both cases will raise an error in the main process after the child process
touches the global cache variable, which is shared with the child process.

Users should avoid this kind of usage and a correct usage example is available
in `examples/lmdb_cache/correct_usage_case.py`.

For a more convenient cache mechanism that works well with multiprocessing,
consider using:

- joblib.Memory: a file-lock-free caching mechanism that works well in Linux.

Note: `lmdb` can be used safely with `spawn` because `spawn` does not share the
memory space of the parent process with the child processes. However, `spawn` is
not the default start method in Linux. Users should be aware of this limitation.
"""

import lmdb
from alpenstock.lmdb_cache import LMDBCache
import multiprocessing as mp
import tempfile

global_cache = None

### Failure case 1: Deliberately closing the shared LMDB environment in a child process

def deliberately_close_shared_lmdb_in_child():
    # Initialize the global_cache in the main process, which
    # is then shared with the forked child processes.
    temp_dir = tempfile.mkdtemp()
    global global_cache
    global_cache = LMDBCache(path=temp_dir)
    global_cache.put("key1", "value1")

    print(f"Global cache: key1 = {global_cache.get('key1')}")

    # Make sure that fork is used for process creation
    mp.set_start_method('fork', force=True)

    # Fork a new process
    process = mp.Process(target=child_proc_close_cache_intentionally)
    process.start()
    process.join()

    # Check if the global_cache is still accessible in the main process
    try:
        print(f"Main process after child: key1 = {global_cache.get('key1')}")
    except lmdb.BadRslotError as e:
        print("The global cache is no longer accessible in the main process after the child process closed it.")
        print(f"A `lmdb.BadRslotError` is raised: {e}")


def child_proc_close_cache_intentionally():
    # Close the global cache in the child process
    global global_cache
    global_cache.env.close()


### Failure case 2: Unintentionally closing the shared LMDB environment in a child process

def unintentionally_close_shared_lmdb_in_child():
    # Initialize the global_cache in the main process, which
    # is then shared with the forked child processes.
    temp_dir = tempfile.mkdtemp()
    global global_cache
    global_cache = LMDBCache(path=temp_dir)
    global_cache.put("key1", "value1")

    print(f"Global cache: key1 = {global_cache.get('key1')}")

    # Make sure that fork is used for process creation
    mp.set_start_method('fork', force=True)

    # Fork a new process
    process = mp.Process(target=child_proc_unintentionally_close_cache)
    process.start()
    process.join()

    # Check if the global_cache is still accessible in the main process
    try:
        print(f"Main process after child: key1 = {global_cache.get('key1')}")
    except lmdb.BadRslotError as e:
        print("The global cache is no longer accessible in the main process after the child process closed it.")
        print(f"A `lmdb.BadRslotError` is raised: {e}")


def child_proc_unintentionally_close_cache():
    # Subjectively, the child process do not wish to break the global cache, so
    # decide to nullify the global_cache variable so that it is no longer
    # accessible.
    global global_cache
    global_cache = None

    # However, this will cause the GC to close the LMDB environment and
    # eventually raise a `lmdb.BadRslotError` in the main process. Here, we
    # trigger a GC manually to emulate the behavior.
    import gc
    gc.collect()

    # The problem arises from the fact that `py-lmdb` closes the LMDB
    # environment in the `__del__` (see `lmdb.Environment.__del__`). As we
    # cannot modify the `py-lmdb` library, we cannot prevent this behavior.
    #
    # So, please open the LMDBCache inside the child process if you want to use
    # it there, and avoid opening it in the main process.


if __name__ == "__main__":
    # deliberately_close_shared_lmdb_in_child()
    unintentionally_close_shared_lmdb_in_child()
