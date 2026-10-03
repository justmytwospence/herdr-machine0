import unittest

from herdr_machine0 import idle


def slot(status, **tokens):
    return {"slot": "s", "agent_status": status, "tokens": tokens}


class IdleTest(unittest.TestCase):
    def test_all_quiet(self):
        self.assertEqual(idle.idle_now([slot("idle"), slot("done")], 0.1, False, 0.3), (True, "idle"))

    def test_busy_states(self):
        for status in ("working", "blocked", "unknown", None):
            self.assertFalse(idle.idle_now([slot(status)], 0.0, False, 0.3)[0], status)

    def test_background_work_and_activity_keep_awake(self):
        self.assertFalse(idle.idle_now([slot("idle", bg="1")], 0.0, False, 0.3)[0])
        self.assertFalse(idle.idle_now([slot("done", activity="working")], 0.0, False, 0.3)[0])

    def test_load_and_keep_awake(self):
        self.assertFalse(idle.idle_now([slot("idle")], 0.5, False, 0.3)[0])
        self.assertFalse(idle.idle_now([slot("idle")], None, False, 0.3)[0])
        self.assertEqual(idle.idle_now([slot("idle")], 0.0, True, 0.3), (False, "keep-awake"))

    def test_no_panes_counts_as_idle(self):
        self.assertTrue(idle.idle_now([], 0.0, False, 0.3)[0])

    def test_decide(self):
        self.assertEqual(idle.decide(False, 100.0, 200.0, 1), (False, None))
        self.assertEqual(idle.decide(True, None, 1000.0, 120), (False, 1000.0))
        self.assertEqual(idle.decide(True, 1000.0, 1000.0 + 119 * 60, 120), (False, 1000.0))
        self.assertEqual(idle.decide(True, 1000.0, 1000.0 + 120 * 60, 120), (True, 1000.0))

    def test_loadavg(self):
        self.assertEqual(idle.parse_loadavg("0.10 0.20 0.30 1/200 1234"), 0.30)
        self.assertIsNone(idle.parse_loadavg(""))


if __name__ == "__main__":
    unittest.main()
