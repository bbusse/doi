'''
Tests for doi.cache, the url keyed file store
'''

import os
import tempfile
import unittest
from unittest import mock

from doi.cache import FileCache


class TestFileCache(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="doi-cache-")
        self.cache = FileCache(self.dir)

    def test_missing_key(self):
        self.assertIsNone(self.cache.path("http://img/none"))
        self.assertIsNone(self.cache.get("http://img/none"))
        self.assertIsNone(FileCache(os.path.join(self.dir, "absent")).path("k"))

    def test_put_then_path_and_get(self):
        path = self.cache.put("http://img/a", b"bytes", ".jpg")
        self.assertTrue(path.endswith(".jpg"))
        self.assertEqual(self.cache.path("http://img/a"), path)
        self.assertEqual(self.cache.get("http://img/a"), b"bytes")
        self.assertEqual(sorted(os.listdir(self.dir)), [os.path.basename(path)])

    def test_keys_do_not_collide(self):
        a = self.cache.put("http://img/a", b"a")
        b = self.cache.put("http://img/b", b"b")
        self.assertNotEqual(a, b)
        self.assertEqual(self.cache.get("http://img/a"), b"a")

    def test_empty_entry_is_not_served(self):
        self.cache.put("http://img/a", b"")
        self.assertIsNone(self.cache.path("http://img/a"))

    def test_failed_write_leaves_nothing(self):
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.cache.put("http://img/a", b"bytes")
        self.assertEqual(os.listdir(self.dir), [])

    def test_prune_drops_the_oldest_over_the_cap(self):
        cache = FileCache(self.dir, max_bytes=10)
        first = cache.put("http://img/1", b"12345")
        os.utime(first, (1, 1))
        second = cache.put("http://img/2", b"12345")
        os.utime(second, (2, 2))
        third = cache.put("http://img/3", b"12345")
        self.assertFalse(os.path.exists(first))
        self.assertTrue(os.path.exists(second))
        self.assertTrue(os.path.exists(third))

    def test_oversize_entry_survives_its_own_prune(self):
        cache = FileCache(self.dir, max_bytes=4)
        path = cache.put("http://img/big", b"0123456789")
        self.assertTrue(os.path.exists(path))

    def test_entry_pruned_mid_lookup(self):
        # listed, then removed by another thread before it is stat'ed
        gone = mock.Mock()
        gone.name = self.cache.name("http://img/a") + ".jpg"
        gone.stat.side_effect = FileNotFoundError
        with mock.patch.object(self.cache, "entries", return_value=[gone]):
            self.assertIsNone(self.cache.path("http://img/a"))

    def test_prune_skips_an_entry_removed_mid_scan(self):
        gone = mock.Mock()
        gone.stat.side_effect = FileNotFoundError
        with mock.patch.object(self.cache, "entries", return_value=[gone]):
            self.cache.prune()

    def test_directory_from_env(self):
        with mock.patch.dict(os.environ, {"CACHE_DIR": self.dir}):
            self.assertEqual(FileCache().directory, self.dir)


if __name__ == "__main__":
    unittest.main()
