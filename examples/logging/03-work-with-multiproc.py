from alpenstock.logging import LoguruInitalizer, logger
import multiprocessing as mp
from pathlib import Path
import os
import tempfile
import time
from random import random

### Example 1: create sub-processes manually


def sub_proc_task(tmp_dir: str):
    tmp_dir = Path(tmp_dir)
    pid = os.getpid()

    # Initialize logger once per worker process
    (
        LoguruInitalizer()
        .preset_brief()
        .serialize_to_file(tmp_dir / f"log-{pid}.json")
        .initialize(on_reinitialize="abort")
    )

    logger.info(f"Worker {pid} started")
    logger.debug(f"Worker {pid} is doing some work")
    logger.info(f"Worker {pid} finished work")

    # Without this line, some log messages may be missing
    logger.complete()


def create_sub_proc_manually():
    LoguruInitalizer().preset_brief().initialize(on_reinitialize="abort")

    tmp_dir = tempfile.mkdtemp(prefix="alpenstock-")
    logger.info(f"Temporary directory created: {tmp_dir}")

    # Compatible with spawn, fork and forkserver methods
    mp.set_start_method("fork", force=True)

    processes = []
    for _ in range(4):  # Create 4 worker processes
        p = mp.Process(target=sub_proc_task, args=(tmp_dir,))
        p.start()
        processes.append(p)

    for p in processes:
        p.join()  # Wait for all processes to finish

    logger.info("All worker processes have completed.")


### Example 2: Working with a process pool


def pool_task(x):
    ## DO NOT initialize logger here, as a process pool may reuse the same worker
    # LoguruInitalizer().preset_brief().initialize(on_reinitialize='abort')

    time.sleep(0.05 + 0.05 * random())  # Simulate some work

    pid = os.getpid()
    logger.info(f"Worker {pid} processing {x}")

    logger.complete()  # Ensure all log messages are flushed
    return x * x  # Example computation


def worker_initializer():
    # Initialize logger here
    (
        LoguruInitalizer()
        .preset_brief()
        # Disable enqueue for the combination of `Linux` + `spawn`/`forkserver`
        # + `mp.Pool`, or the internal semaphores of Loguru will not be cleaned
        # up properly, and a warning message will be printed at shutdown like
        # this:
        #
        #   resource_tracker.py:301: UserWarning: resource_tracker: There appear
        #   to be 8 leaked semaphore objects to clean up at shutdown: ...
        #
        # .set_enqueue(False)
        .initialize(on_reinitialize="abort")
    )


def demo_with_proc_pool():
    LoguruInitalizer().preset_brief().initialize(on_reinitialize="abort")

    # Compatibility: everything works fine except for the combination of Linux +
    # spawn/forkserver + mp.Pool, which requires to disable the enqueue mode of
    # Loguru to avoid its internal semaphores from being leaked. See:
    # worker_initializer for details.
    #
    # Luckily, Windows + spawn + mp.Pool works without any issues, and in Linux,
    # fork is the preferred method. So it is rare to encounter this issue in
    # practice, and even if you do, it is easy to fix by disabling the enqueue
    # mode.

    # Force a specific start method for testing
    # mp.set_start_method("fork", force=True)

    with mp.Pool(
        processes=4,
        initializer=worker_initializer,  # Register the initializer function here
    ) as pool:
        results = pool.map(pool_task, range(10))
        logger.info(f"Results: {results}")

    logger.info("Process pool has completed.")


if __name__ == "__main__":
    ## Example 1
    # create_sub_proc_manually()

    ## Example 2
    demo_with_proc_pool()
