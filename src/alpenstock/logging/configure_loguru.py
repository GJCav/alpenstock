from loguru import logger
import sys
from typing import Literal
import warnings
import os
from pydantic import validate_call

LogLevel = Literal["TRACE", "DEBUG", "INFO", "WARNING"]

_TIME_SEC = "<green>{time:MM-DD HH:mm:ss}</green>"
_DATETIME_SEC = "<green>{time:YYYY-MM-DD HH:mm:ss}</green>"
_LVL_SEC = "<lvl><b>{level: <7}</b></lvl>"
_LOC_SEC = "<lvl><n>{module}:{function}:{line}</n></lvl>"
_THD_SEC = "{thread.name}"
_MSG_SEC = "<lvl><n>{message}</n></lvl>"

# Module-level flag to track initialization per PID. This is to prevent
# re-initialization of Loguru in the same process, which could lead to
# unexpected behavior or configuration loss.
#
# Tracking the PID supports both the fork and spawn methods of multiprocessing,
# ensuring that each process can have its own Loguru configuration without
# interfering with others.
_loguru_initialized_pids = {}


class LoguruInitalizer:
    _BRIEF_FMT_SECTIONS = [_TIME_SEC, _LVL_SEC, _MSG_SEC]
    _FULL_FMT_SECTIONS = [_TIME_SEC, _LVL_SEC, _LOC_SEC, _THD_SEC, _MSG_SEC]
    BRIEF_FMT = "|".join(_BRIEF_FMT_SECTIONS)
    FULL_FMT = "|".join(_FULL_FMT_SECTIONS)

    def __init__(self):
        self._fmt_sections = []
        self._level = "INFO"
        
        self._serialize_to_file = None
        self._enqueue = True
    

    @validate_call
    def initialize(
            self, 
            on_reinitialize: Literal["overwrite", "warn", "abort", "ignore"] = "warn",
        ) -> None:
        global _loguru_initialized_pids
        pid = os.getpid()
        reinitialized = _loguru_initialized_pids.get(pid, False)

        if reinitialized:
            if on_reinitialize == "abort":
                raise RuntimeError(
                    "Loguru has already been initialized in this process. "
                    "Re-initializing is not allowed."
                )
            elif on_reinitialize == "warn":
                warnings.warn(
                    "Loguru has already been initialized in this process. "
                    "Re-initializing will overwrite the previous configuration.",
                    UserWarning
                )
            elif reinitialized and on_reinitialize == "overwrite":
                pass
            elif on_reinitialize == "ignore":
                return None

        msg_format = "|".join(self._fmt_sections)

        logger.remove()

        logger.add(
            sys.__stderr__,
            colorize=True,
            level=self._level, 
            enqueue=self._enqueue,
            format=msg_format
        )

        if self._serialize_to_file:
            logger.add(
                **self._serialize_to_file,
                format=msg_format
            )

        logger.level("INFO", color="")
        logger.level("DEBUG", color="<fg #9fcce0>")
        logger.level("TRACE", color="<light-black>")

        _loguru_initialized_pids[pid] = True
        return None

    
    def preset_brief(self):
        self._fmt_sections = self._BRIEF_FMT_SECTIONS.copy()

        self._level = "INFO"
        return self
    

    def preset_full(self):
        self._fmt_sections = self._FULL_FMT_SECTIONS.copy()
        self._level = "DEBUG"
        return self


    @validate_call
    def set_level(self, level: LogLevel | int):
        self._level = level
        return self
    
    
    @validate_call
    def set_enqueue(self, enqueue: bool = True):
        self._enqueue = enqueue
        return self


    def serialize_to_file(
            self, 
            file_path: str, 
            level: LogLevel | int = "DEBUG",
            enqueue: bool = True,
            rotation: str = None,
            rentention: str = None,
            compression: str = None
    ):
        self._serialize_to_file = dict(
            sink=file_path,
            level=level,
            enqueue=enqueue,
            colorize=False,

            serialize=True,
            rotation=rotation,
            retention=rentention,
            compression=compression
        )
        return self