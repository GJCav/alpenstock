import lmdb
from alpenstock.lmdb_cache import LMDBCache
import multiprocessing as mp
import tempfile

### Failure case 2:
###   Unintentionally closing the shared LMDB environment in a child process

global_cache = None


def unintentionally_close_shared_lmdb_in_child():
    # Initialize the global_cache in the main process, which
    # is then shared with the forked child processes.
    temp_dir = tempfile.mkdtemp()
    global global_cache
    global_cache = LMDBCache(path=temp_dir)
    global_cache.put("key1", "value1")

    print(f"Global cache: key1 = {global_cache.get('key1')}")

    # Make sure that fork is used for process creation
    mp.set_start_method("fork", force=True)

    # Fork a new process
    process = mp.Process(target=child_proc_unintentionally_close_cache)
    process.start()
    process.join()

    # Check if the global_cache is still accessible in the main process
    try:
        print(f"Main process after child: key1 = {global_cache.get('key1')}")
    except lmdb.BadRslotError as e:
        print(
            "The global cache is no longer accessible in the main process after the child process closed it."
        )
        print(f"A `lmdb.BadRslotError` is raised: {e}")


def child_proc_unintentionally_close_cache():
    # Subjectively, the child process do not wish to break the global cache,
    # deciding to nullify the global_cache variable so that it is no longer
    # accessible.
    global global_cache
    global_cache = None

    # However, this will cause the GC to close the LMDB environment and
    # eventually raise a `lmdb.BadRslotError` in the main process. Here, we
    # trigger a GC manually to emulate the behavior.
    import gc

    gc.collect()


if __name__ == "__main__":
    unintentionally_close_shared_lmdb_in_child()
