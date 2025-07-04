from alpenstock.lmdb_cache import LMDBCache
from pydantic import BaseModel
import multiprocessing as mp
import time
from random import random
import tempfile

class TaskDescriptor(BaseModel):
    key: str            # identifier for the computation task
    result: str | None = None  # result of the computation task


def worker(task_list: list[TaskDescriptor], cache_params) -> list[TaskDescriptor]:
    print(f"Worker started with {len(task_list)} tasks.")
    with LMDBCache(**cache_params) as cache:
        for task in task_list:
            cached_result = cache.get(task.key)
            if cached_result is not None:
                # If the result is already cached, skip computation
                print(f"Cache hit for {task.key}, skipping computation.")
                task.result = cached_result.result
            else:
                # Simulate some computation
                result = f"Compute result for {task.key}"
                task.result = result

                time.sleep(0.1 + random() * 0.4) # Simulate variable computation time

                # Store the result in the cache
                print(f"Caching result for {task.key}")
                cache.put(task.key, task)
    return task_list


def main():
    # Create tasks
    tasks = [TaskDescriptor(key=f"task_{i}") for i in range(8)]
    tasks.extend([TaskDescriptor(key=f"task_{i}") for i in range(3, 8)]) # Some tasks overlap
    tasks.extend([TaskDescriptor(key=f"task_{i}") for i in range(10)]) # More overlapping tasks

    # Split tasks into batches for parallel processing
    batch_size = 3
    batches = [tasks[i:i + batch_size] for i in range(0, len(tasks), batch_size)]

    # Start processing    
    cache_params = {"path": tempfile.mkdtemp(), "map_size": (1024**2) * 512}  # 512 MB cache
    with mp.Pool(processes=4) as pool:
        results = pool.starmap(worker, [(batch, cache_params) for batch in batches])
    
    # Flatten the results
    results = [task for batch in results for task in batch]
    print("\n\nAll tasks processed. Results:")
    for task in results:
        print(f"{task.key}: {task.result}")


if __name__ == "__main__":
    main()