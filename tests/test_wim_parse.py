import unittest

from windows_vm.media import MediaError, resolve_wim_index

INFO = """
Filesystem manifest:
...
Index:          1
Name:           Windows 11 Home
...
Index:          6
Name:           Windows 11 Pro
...
Index:          11
Name:           Windows 11 Education
"""


class TestResolveWimIndex(unittest.TestCase):
    def test_resolves_pro(self):
        self.assertEqual(resolve_wim_index(INFO, "Windows 11 Pro"), 6)

    def test_case_insensitive_substring(self):
        self.assertEqual(resolve_wim_index(INFO, "pro"), 6)

    def test_unknown_edition_raises(self):
        with self.assertRaises(MediaError):
            resolve_wim_index(INFO, "Windows 11 Enterprise LTSC")


if __name__ == "__main__":
    unittest.main()
