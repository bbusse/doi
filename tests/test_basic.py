import unittest

class TestBasic(unittest.TestCase):
    def test_import(self):
        import doi
        self.assertTrue(hasattr(doi, "__version__"))

if __name__ == "__main__":
    unittest.main()
