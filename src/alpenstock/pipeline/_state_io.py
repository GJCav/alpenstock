from __future__ import annotations

import os
import pickle
import tempfile
from pathlib import Path
from typing import Any, Callable



def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise



def atomic_write_text(path: Path, payload: str) -> None:
    atomic_write_bytes(path, payload.encode("utf-8"))



def atomic_write_via(path: Path, writer: Callable[[Any], None], *, binary: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    mode = "wb" if binary else "w"
    encoding = None if binary else "utf-8"
    try:
        with os.fdopen(fd, mode, encoding=encoding) as f:
            writer(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise



def default_saver(path: Path, obj: Any) -> None:
    atomic_write_bytes(path, pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL))



def default_loader(path: Path) -> Any:
    return pickle.loads(path.read_bytes())
