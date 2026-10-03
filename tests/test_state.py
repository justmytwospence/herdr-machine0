import json
import os
import unittest

from herdr_machine0 import config, registry, spokerun, sshconf


class ConfigTest(unittest.TestCase):
    def test_env_roundtrip_and_filter(self):
        values = {"ANTHROPIC_OAUTH_TOKEN": "sk-ant-oat01-x'y", "MACHINE0_API_TOKEN": "m0", "MODEL_API_KEY": "k"}
        parsed = config.parse_env_file(config.render_env_file(values))
        self.assertEqual(parsed["MODEL_API_KEY"], "k")
        self.assertEqual(config.spoke_secrets(values).keys(), {"ANTHROPIC_OAUTH_TOKEN", "MODEL_API_KEY"})

    def test_secrets_file_private(self):
        config.write_secrets({"A": "b"})
        self.assertEqual(os.stat(config.SECRETS_FILE).st_mode & 0o777, 0o600)
        self.assertEqual(config.read_secrets(), {"A": "b"})

    def test_parse_handles_quotes_and_export(self):
        self.assertEqual(config.parse_env_file('export A="x y"\n# c\nB=1\nbad line\n'), {"A": "x y", "B": "1"})


class SshConfTest(unittest.TestCase):
    def test_update_parse_remove(self):
        self.assertTrue(sshconf.update("demo", "1.2.3.4", "ubuntu"))
        self.assertFalse(sshconf.update("demo", "1.2.3.4", "ubuntu"))
        sshconf.update("other", "5.6.7.8", "ubuntu")
        self.assertTrue(sshconf.update("demo", "9.9.9.9", "ubuntu"))
        text = sshconf.read()
        self.assertEqual(sshconf.parse(text), {"demo": "9.9.9.9", "other": "5.6.7.8"})
        self.assertIn("HostKeyAlias m0-demo", text)
        sshconf.ensure_include()
        with open(os.path.expanduser("~/.ssh/config")) as f:
            self.assertTrue(f.read().startswith(sshconf.INCLUDE))
        sshconf.remove("demo")
        self.assertEqual(sshconf.parse(sshconf.read()), {"other": "5.6.7.8"})


class RegistryTest(unittest.TestCase):
    def test_slots_and_next(self):
        registry.put_slot("a", "main", harness="pi", cwd="~/x")
        registry.put_slot("a", "main", pane_id="w1:p1")
        self.assertEqual(registry.get_slot("a", "main")["cwd"], "~/x")
        self.assertEqual(registry.next_slot("a"), "main-2")
        self.assertEqual(registry.next_slot("a", "fix"), "fix")
        registry.put_spoke("a", size="large")
        registry.drop_spoke("a")
        self.assertIsNone(registry.get_slot("a", "main"))

    def test_lock_busy_then_released(self):
        one = registry.SlotLock("b", "main")
        one.acquire()
        two = registry.SlotLock("b", "main")
        with self.assertRaises(registry.SlotBusy):
            two.acquire()
        one.release()
        two.acquire()
        two.release()


class SpokeRunTest(unittest.TestCase):
    def test_resume_table(self):
        self.assertEqual(spokerun.harness_command("pi", None), ["pi"])
        self.assertEqual(spokerun.harness_command("pi", "/s.jsonl"), ["pi", "--session", "/s.jsonl"])
        self.assertEqual(spokerun.harness_command("claude", "id"), ["claude", "--resume", "id"])
        self.assertEqual(spokerun.harness_command("codex", "id"), ["codex", "resume", "id"])
        self.assertEqual(spokerun.harness_command("opencode", None), ["opencode"])
        with self.assertRaises(ValueError):
            spokerun.harness_command("vim", None)

    def test_seed_pi_keeps_other_entries_and_never_stores_refresh(self):
        agent = os.path.expanduser("~/.pi/agent-seed")
        os.makedirs(agent, exist_ok=True)
        with open(os.path.join(agent, "auth.json"), "w") as f:
            json.dump({"meta": {"type": "api_key", "key": "k"}}, f)
        spokerun.seed_pi("openai-codex", {"type": "oauth", "access": "a", "refresh": "REAL", "expires": 1}, agent)
        with open(os.path.join(agent, "auth.json")) as f:
            store = json.load(f)
        self.assertEqual(store["meta"]["key"], "k")
        self.assertEqual(store["openai-codex"]["refresh"], spokerun.SENTINEL)
        self.assertEqual(os.stat(os.path.join(agent, "auth.json")).st_mode & 0o777, 0o600)

    def test_seed_codex_cli(self):
        home = os.path.expanduser("~/.codex-seed")
        spokerun.seed_codex_cli({"access": "a"}, home)  # no dir: no-op
        self.assertFalse(os.path.exists(home))
        os.makedirs(home)
        spokerun.seed_codex_cli({"access": "a", "accountId": "acct"}, home)
        with open(os.path.join(home, "auth.json")) as f:
            data = json.load(f)
        self.assertEqual(data["tokens"]["refresh_token"], spokerun.SENTINEL)
        self.assertEqual(data["tokens"]["account_id"], "acct")


if __name__ == "__main__":
    unittest.main()
