import pickle
from typing import Any

try:
    import lmdb
except ImportError:
    raise ImportError(
        "Please install the 'lmdb' package to use LMDBCache. "
        "You can do this by running 'pip install lmdb'."
    )


class LMDBCache:
    def __init__(
            self, 
            path: str, 
            map_size: int = (1024**2) * 512 # Default to 512 MB
        ): 
        self.env = lmdb.open(path, map_size=map_size)

    def put(self, key, value):
        with self.env.begin(write=True) as txn:
            txn.put(
                pickle.dumps(key),
                pickle.dumps(value)
            )

    def get(self, key) -> Any | None:
        with self.env.begin(write=False) as txn:
            value = txn.get(pickle.dumps(key), default=None)
        
        if value is None:
            return None
        
        return pickle.loads(value)
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_value, traceback):
        self.env.close()
