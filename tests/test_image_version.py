import tests  # noqa: F401  first, so HOME is a throwaway dir before anything reads it
import unittest

from herdr_machine0.lifecycle import new_version


class NewVersionTest(unittest.TestCase):
    def test_version_list_gain_wins(self):
        self.assertEqual(new_version("saved", {1, 2}, [1, 2, 3]), 3)

    def test_output_without_draft_marker(self):
        self.assertEqual(new_version("Saved m0-spoke v7", set(), []), 7)

    def test_draft_marker_still_parsed(self):
        self.assertEqual(new_version("m0-spoke v4 (draft)", {1, 2, 3}, [1, 2, 3]), 4)

    def test_unknown(self):
        self.assertIsNone(new_version("ok", {1}, [1]))


if __name__ == "__main__":
    unittest.main()
