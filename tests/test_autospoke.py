import tests  # noqa: F401  first, so HOME is a throwaway dir before anything reads it
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

    def test_handle_hands_the_pane_to_the_picker(self):
        server = FakeHerdrServer({"pane.list": {"panes": [
            {"pane_id": "w5:p1", "workspace_id": "w5"}, {"pane_id": "w1:p1", "workspace_id": "w1"}]}})
        try:
            self.assertEqual(autospoke.handle(event(), server.path), "w5")
            typed = [r["params"]["text"] for r in server.requests if r["method"] == "pane.send_text"]
            self.assertEqual(typed, ['eval "$(spoke pick-space --workspace w5)"'])
            # A space labelled with a spoke's name is that spoke's: not handled.
            registry.put_spoke("myrepo", repo="o/myrepo")
            server.requests.clear()
            self.assertIsNone(autospoke.handle(event("w6", "myrepo"), server.path))
            self.assertEqual(server.requests, [])
        finally:
            server.close()

    def test_garbage_and_wrong_role(self):
        self.assertIsNone(autospoke.handle("not json"))
        self.assertIsNone(autospoke.handle(json.dumps({"data": {}})))


class ReposTest(unittest.TestCase):
    def test_normalize(self):
        from herdr_machine0 import repos
        for raw in ("o/r", "https://github.com/o/r", "https://github.com/o/r.git", "git@github.com:o/r.git"):
            self.assertEqual(repos.normalize(raw), "o/r", raw)
        self.assertIsNone(repos.normalize("not a repo"))

    def test_names_and_choices(self):
        from herdr_machine0 import repos
        self.assertEqual(repos.slug("Herdr_Machine0"), "herdr-machine0")
        self.assertEqual(repos.slug("2fast"), "r-2fast")
        self.assertEqual(repos.branch_slot("feature/Big-Thing"), "big-thing")
        registry.put_spoke("dotfiles", repo="me/dotfiles")
        self.assertEqual(repos.spoke_for("me/dotfiles"), "dotfiles")
        self.assertEqual(repos.spoke_name("other/dotfiles"), "other-dotfiles")
        repos.store([{"nameWithOwner": "me/zeta", "pushedAt": "2026-01-02"},
                     {"nameWithOwner": "me/dotfiles", "pushedAt": "2026-01-03"},
                     {"nameWithOwner": "bad name"}])
        got = [(c["repo"], c["spoke"]) for c in repos.choices()]
        self.assertIn(("me/dotfiles", "dotfiles"), got)
        self.assertIn(("me/zeta", None), got)
        self.assertLess(got.index(("me/dotfiles", "dotfiles")), got.index(("me/zeta", None)))


class SpaceTest(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        self.server = FakeHerdrServer({"workspace.list": {"workspaces": []}, "tab.list": {"tabs": [{"tab_id": "w7:t1"}]}})
        self.env = mock.patch.dict("os.environ", {"HERDR_SOCKET_PATH": self.server.path})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.server.close()

    def pick(self, choice):
        from unittest import mock
        from herdr_machine0 import space
        with mock.patch("herdr_machine0.picker.pick", return_value=choice):
            return space.pick_space("w7")

    def test_cancel_keeps_a_hub_shell(self):
        from herdr_machine0 import space
        self.assertEqual(self.pick(None), space.KEEP_HUB)

    def test_new_repo_creates_its_spoke_here(self):
        code = self.pick({"kind": "repo", "repo": "me/newrepo", "spoke": None})
        self.assertIn("spoke new newrepo --in-pane --repo me/newrepo", code)
        self.assertIn(config.slot_dir("newrepo", "main"), code)
        info = registry.load()["spokes"]["newrepo"]
        self.assertEqual((info["repo"], info["workspace_id"]), ("me/newrepo", "w7"))
        renames = [r["params"] for r in self.server.requests if r["method"].endswith(".rename")]
        self.assertIn({"workspace_id": "w7", "label": "newrepo"}, renames)
        self.assertIn({"tab_id": "w7:t1", "label": "main"}, renames)

    def test_existing_repo_attaches_here(self):
        registry.put_spoke("oldrepo", repo="me/oldrepo", harness="claude")
        registry.put_slot("oldrepo", "main", harness="claude", cwd="~/Projects/oldrepo")
        code = self.pick({"kind": "repo", "repo": "me/oldrepo", "spoke": "oldrepo"})
        self.assertIn("spoke attach oldrepo main --harness claude --cwd '~/Projects/oldrepo'", code)


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
