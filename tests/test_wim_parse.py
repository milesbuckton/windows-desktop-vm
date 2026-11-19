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
Index:          7
Name:           Windows 11 Pro N
...
Index:          11
Name:           Windows 11 Education
...
Index:          12
Name:           Windows 11 Pro Education
"""


class TestResolveWimIndex(unittest.TestCase):
    def test_resolves_pro(self):
        self.assertEqual(resolve_wim_index(INFO, "Windows 11 Pro"), 6)

    def test_exact_match_case_insensitive(self):
        self.assertEqual(resolve_wim_index(INFO, "windows 11 pro"), 6)
        self.assertEqual(resolve_wim_index(INFO, "Windows 11 Pro N"), 7)

    def test_unique_substring(self):
        self.assertEqual(resolve_wim_index(INFO, "pro education"), 12)

    def test_ambiguous_substring_raises(self):
        with self.assertRaises(MediaError):
            resolve_wim_index(INFO, "pro")

    def test_unknown_edition_raises(self):
        with self.assertRaises(MediaError):
            resolve_wim_index(INFO, "Windows 11 Enterprise LTSC")


if __name__ == "__main__":
    unittest.main()
