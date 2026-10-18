'''
A content cache keyed by url: downloaded pictures, encoded qr codes. Every
consumer wants a file to open, so an entry is a local file whatever the store,
and path() is how a caller gets at it. The filesystem store is store and path
in one. Another store goes in as a class with the same three methods that
materialises its bytes into a local file on path()
'''

import hashlib
import logging
import os
import stat
import tempfile
import time


class FileCache:

    # Bounded by size, oldest out first: on an appliance the temporary
    # directory is tmpfs, so a picture for every url a feed ever surfaced
    # would otherwise sit in memory for the uptime of the device
    max_bytes = 64 << 20

    # A partial write older than this is from a process that died mid write
    part_ttl_s = 3600

    def __init__(self, directory=None, max_bytes=None):
        self.directory = (directory or os.environ.get("CACHE_DIR")
                          or os.path.join(tempfile.gettempdir(),
                                          f"doi-cache-{os.geteuid()}"))
        if max_bytes is not None:
            self.max_bytes = max_bytes
        self._warned = False

    # Private to this user, so nothing planted by someone else is ever served.
    # Anyone can pre-create a predictable name in a shared temporary
    # directory. A missing directory is fine, put() creates it
    def trusted(self):
        try:
            st = os.lstat(self.directory)
        except FileNotFoundError:
            return True
        except OSError:
            return False

        ok = (stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid()
              and not st.st_mode & 0o022)
        if not ok and not self._warned:
            self._warned = True
            logging.warning(f"cache: {self.directory} is not a private "
                            "directory of this user, not used")

        return ok

    @staticmethod
    def name(key):
        return hashlib.md5(key.encode()).hexdigest()

    def scan(self):
        if not self.trusted():
            return []
        try:
            return [e for e in os.scandir(self.directory) if e.is_file()]
        except OSError:
            return []

    def entries(self):
        return [e for e in self.scan() if not e.name.endswith(".part")]

    @staticmethod
    def remove(path):
        try:
            os.remove(path)
        except OSError:
            pass

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
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        if not self.trusted():
            raise PermissionError(f"{self.directory} is not private")
        path = os.path.join(self.directory, self.name(key) + suffix)
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except OSError:
            self.remove(tmp)
            raise
        self.prune(keep=path)

        return path

    # Newest first until the cap is passed, the rest go. keep is never
    # removed, so a file over the cap on its own is still there for the
    # caller that was just handed its path. Abandoned partial writes go too
    def prune(self, keep=None):
        sized = []
        for entry in self.scan():
            try:
                st = entry.stat()
            except OSError:
                continue
            if entry.name.endswith(".part"):
                if time.time() - st.st_mtime > self.part_ttl_s:
                    self.remove(entry.path)
                continue
            sized.append((st.st_mtime, st.st_size, entry.path))

        total = 0
        for _, size, path in sorted(sized, reverse=True):
            total += size
            if total > self.max_bytes and path != keep:
                self.remove(path)


cache = FileCache()
