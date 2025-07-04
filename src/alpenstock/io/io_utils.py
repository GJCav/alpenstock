from contextlib import contextmanager
import sys

class TeeStream:
    def __init__(self, streams=[], autoflush=True) -> None:
        self.streams = streams
        self.autoflush = autoflush

    def write(self, message):
        for stream in self.streams:
            stream.write(message)
            if self.autoflush:
                stream.flush()

    def flush(self):
        for stream in self.streams:
            stream.flush()

    def close(self):
        """
        Do nothing here, as we do not want to close the streams.

        This is to prevent closing sys.stdout or any other stream that might be
        passed to this class.

        Users can close the streams manually if needed by iterating over
        `self.streams`.
        """
        pass


@contextmanager
def tee_stdout_to_file(stdout_file, mode='a'):
    """
    Context manager to tee stdout to a file.

    Args:
        stdout_file (str): Path to the file to write stdout to.
        mode (str): Mode in which to open the file (default: 'a').
    """
    file = open(stdout_file, mode)
    tee_stream = TeeStream([file, sys.stdout], autoflush=True)
    original_stdout = sys.stdout
    sys.stdout = tee_stream
    try:
        yield None
    finally:
        sys.stdout = original_stdout
        file.close()