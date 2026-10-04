import json
import random
import unittest

from herdr_machine0 import autospoke, config, registry
from tests.test_hub import FakeHerdrServer


def event(ws_id="w5", label="exedev", panes=1, tabs=1, **extra):
    ws = dict({"workspace_id": ws_id, "label": label, "pane_count": panes, "tab_count": tabs}, **extra)
    return json.dumps({"event": "workspace_created", "data": {"type": "workspace_created", "workspace": ws}})


class AutoSpokeTest(unittest.TestCase):
    def test_names_avoid_taken(self):
        rng = random.Random(1)
        first = autospoke.new_name([], rng)
        self.assertRegex(first, r"^[a-z]+-[a-z]+$")
        taken = {"%s-%s" % (a, n) for a in autospoke.ADJECTIVES for n in autospoke.NOUNS}
        self.assertEqual(autospoke.new_name(taken, rng), "spoke-2")

    def test_should_handle(self):
        self.assertTrue(autospoke.should_handle({"label": "exedev", "pane_count": 1}, [], True))
        self.assertFalse(autospoke.should_handle({"label": "exedev"}, [], False))
        self.assertFalse(autospoke.should_handle({"label": "demo"}, ["demo"], True))
        self.assertFalse(autospoke.should_handle({"label": "x", "worktree": {"branch": "b"}}, [], True))
        self.assertFalse(autospoke.should_handle({"label": "x", "pane_count": 2}, [], True))

    def test_handle_names_renames_and_starts(self):
        server = FakeHerdrServer({"pane.list": {"panes": [
            {"pane_id": "w5:p1", "workspace_id": "w5"}, {"pane_id": "w1:p1", "workspace_id": "w1"}]}})
        try:
            name = autospoke.handle(event(), server.path)
            self.assertIsNotNone(name)
            methods = [(r["method"], r["params"]) for r in server.requests]
            self.assertIn(("workspace.rename", {"workspace_id": "w5", "label": name}), methods)
            typed = [p["text"] for m, p in methods if m == "pane.send_text"]
            self.assertEqual(len(typed), 1)
            self.assertIn("spoke new %s --in-pane" % name, typed[0])
            self.assertIn(config.slot_dir(name, "main"), typed[0])
            self.assertTrue(registry.load()["spokes"][name]["pending"])
            # The space herdr-machine0 opens for that spoke is not handled again.
            server.requests.clear()
            self.assertIsNone(autospoke.handle(event("w6", name), server.path))
            self.assertEqual(server.requests, [])
        finally:
            server.close()

    def test_garbage_and_wrong_role(self):
        self.assertIsNone(autospoke.handle("not json"))
        self.assertIsNone(autospoke.handle(json.dumps({"data": {}})))


class SetupTest(unittest.TestCase):
    def test_personal_hooks_off_by_default(self):
        self.assertIsNone(config.DEFAULTS["provision_command"])
        self.assertIsNone(config.DEFAULTS["sync_command"])

    def test_doctor_without_a_role_fails(self):
        import io
        import contextlib
        import os
        from unittest import mock
        from herdr_machine0 import setup
        with mock.patch.dict(os.environ, {"HERDR_MACHINE0_ROLE": "unknown"}):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(setup.doctor(), 1)


if __name__ == "__main__":
    unittest.main()
