'''
A content cache keyed by url: downloaded pictures, encoded qr codes. Every
consumer wants a file to open, so an entry is a local file whatever the store,
and path() is how a caller gets at it. The filesystem store is store and path
in one. Another store goes in as a class with the same three methods that
materialises its bytes into a local file on path()
'''

import hashlib
import os
import tempfile


class FileCache:

    # Bounded by size, oldest out first: on an appliance the temporary
    # directory is tmpfs, so a picture for every url a feed ever surfaced
    # would otherwise sit in memory for the uptime of the device
    max_bytes = 64 << 20

    def __init__(self, directory=None, max_bytes=None):
        self.directory = (directory or os.environ.get("CACHE_DIR")
                          or os.path.join(tempfile.gettempdir(), "doi-cache"))
        if max_bytes is not None:
            self.max_bytes = max_bytes

    @staticmethod
    def name(key):
        return hashlib.md5(key.encode()).hexdigest()

    def entries(self):
        try:
            return [e for e in os.scandir(self.directory)
                    if e.is_file() and not e.name.endswith(".part")]
        except OSError:
            return []

    # The file holding key, under whatever suffix it was stored, or None
    def path(self, key):
        name = self.name(key)
        for entry in self.entries():
            if not entry.name.startswith(name):
                continue
            # pruned by another thread between the listing and here
            try:
                if entry.stat().st_size > 0:
                    return entry.path
            except OSError:
                continue

        return None

    def get(self, key):
        path = self.path(key)
        if path is None:
            return None
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None

    # Written to a temporary name and moved into place, so a reader never sees
    # a half-written file and a failed write leaves nothing behind
    def put(self, key, data, suffix=""):
        os.makedirs(self.directory, exist_ok=True)
        path = os.path.join(self.directory, self.name(key) + suffix)
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except OSError:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        self.prune(keep=path)

        return path

    # Newest first until the cap is passed, the rest go. keep is never
    # removed, so a file over the cap on its own is still there for the
    # caller that was just handed its path
    def prune(self, keep=None):
        sized = []
        for entry in self.entries():
            try:
                st = entry.stat()
            except OSError:
                continue
            sized.append((st.st_mtime, st.st_size, entry.path))

        total = 0
        for _, size, path in sorted(sized, reverse=True):
            total += size
            if total > self.max_bytes and path != keep:
                try:
                    os.remove(path)
                except OSError:
                    pass


cache = FileCache()
