import tests  # noqa: F401  first, so HOME is a throwaway dir before anything reads it
import json
import os
import socket
import tempfile
import threading
import unittest
from unittest import mock

from herdr_machine0 import broker, config, hub, registry


class FakeHerdrServer:
    """A unix-socket herdr that answers from a dict and records requests."""

    def __init__(self, answers):
        self.answers = answers
        self.requests = []
        self.path = os.path.join(tempfile.mkdtemp(), "herdr.sock")
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(self.path)
        self.sock.listen(16)
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            f = conn.makefile("rwb")
            line = f.readline()
            req = json.loads(line)
            self.requests.append(req)
            answer = self.answers.get(req["method"], {"type": "ok"})
            if callable(answer):
                answer = answer(req["params"])
            f.write(json.dumps({"id": req["id"], "result": answer}).encode() + b"\n")
            f.flush()
            conn.close()

    def close(self):
        self.sock.close()


class ReconcileTest(unittest.TestCase):
    def test_reattaches_shell_panes_by_cwd(self):
        registry.put_slot("rc", "main", harness="claude", cwd="~/Projects/x")
        registry.put_slot("rc", "pi1", harness="pi", cwd="~", session="/s.jsonl")
        main_dir = config.slot_dir("rc", "main")
        pi_dir = config.slot_dir("rc", "pi1")
        panes = [
            {"pane_id": "w1:p1", "cwd": main_dir, "agent_status": "unknown"},
            {"pane_id": "w1:p2", "cwd": pi_dir, "agent_status": "unknown"},
            {"pane_id": "w1:p3", "cwd": "/elsewhere", "agent_status": "unknown"},
        ]
        server = FakeHerdrServer({
            "pane.list": {"panes": panes},
            "pane.process_info": lambda p: {"process_info": {"shell_pid": 1, "foreground_processes": [{"pid": 1}]}},
        })
        try:
            done = hub.reconcile(server.path)
            self.assertEqual(done, ["rc/main"])
            typed = [r["params"]["text"] for r in server.requests if r["method"] == "pane.send_text"]
            self.assertEqual(typed, ["spoke attach rc main --harness claude --cwd '~/Projects/x'"])
            self.assertEqual(registry.get_slot("rc", "main")["pane_id"], "w1:p1")
            # pi panes are left for herdr's own resume unless asked.
            self.assertEqual(hub.reconcile(server.path, include_resumable=True), ["rc/main", "rc/pi1"])
        finally:
            server.close()

    def test_busy_pane_left_alone(self):
        registry.put_slot("rc2", "main", harness="codex", cwd="~")
        server = FakeHerdrServer({
            "pane.list": {"panes": [{"pane_id": "w2:p1", "cwd": config.slot_dir("rc2", "main")}]},
            "pane.process_info": {"process_info": {"shell_pid": 1, "foreground_processes": [{"pid": 7}]}},
        })
        try:
            self.assertEqual(hub.reconcile(server.path), [])
        finally:
            server.close()


class BrokerTest(unittest.TestCase):
    def test_find_pi_package_walks_up_from_shim(self):
        root = tempfile.mkdtemp()
        pkg = os.path.join(root, "libexec", "lib", "node_modules", "@earendil-works", "pi-coding-agent")
        os.makedirs(os.path.join(pkg, "dist"))
        with open(os.path.join(pkg, "package.json"), "w") as f:
            json.dump({"name": broker.PI_PACKAGE}, f)
        open(os.path.join(pkg, "dist", "index.js"), "w").close()
        os.makedirs(os.path.join(root, "libexec", "bin"))
        real = os.path.join(root, "libexec", "bin", "pi")
        with open(real, "w") as f:
            f.write("#!/usr/bin/env node\n")
        os.makedirs(os.path.join(root, "bin"))
        shim = os.path.join(root, "bin", "pi")
        with open(shim, "w") as f:
            f.write('#!/bin/bash\nexec "%s" "$@"\n' % real)
        os.chmod(shim, 0o755)
        with mock.patch("shutil.which", return_value=shim):
            self.assertEqual(os.path.realpath(broker.find_pi_package()), os.path.realpath(pkg))

    def test_compiled_pi_falls_back_to_the_private_sdk(self):
        # exe.dev's pi: package.json beside a compiled binary, no dist/ to import.
        root = tempfile.mkdtemp()
        with open(os.path.join(root, "package.json"), "w") as f:
            json.dump({"name": broker.PI_PACKAGE}, f)
        binary = os.path.join(root, "pi")
        open(binary, "wb").close()
        sdk = os.path.join(tempfile.mkdtemp(), "sdk")
        os.makedirs(os.path.join(sdk, "dist"))
        with open(os.path.join(sdk, "package.json"), "w") as f:
            json.dump({"name": broker.PI_PACKAGE}, f)
        open(os.path.join(sdk, "dist", "index.js"), "w").close()
        with mock.patch("shutil.which", return_value=binary):
            with self.assertRaises(broker.BrokerError):
                broker.find_pi_package()
            with mock.patch.object(broker, "SDK_DIR", sdk):
                self.assertEqual(broker.find_pi_package(), sdk)

    def test_refuses_a_real_refresh_token(self):
        fake = mock.Mock(returncode=0, stdout=json.dumps({"access": "a", "refresh": "REAL"}), stderr="")
        with mock.patch("subprocess.run", return_value=fake), \
                mock.patch.object(broker, "find_pi_package", return_value="/pi"):
            with self.assertRaises(broker.BrokerError):
                broker.get("openai-codex")

    def test_min_validity_per_provider(self):
        self.assertEqual(broker.min_validity_hours("radius"), 2)
        self.assertEqual(broker.min_validity_hours("openai-codex"), 24)

    def test_only_on_hub(self):
        with mock.patch.dict(os.environ, {"HERDR_MACHINE0_ROLE": "spoke"}):
            with self.assertRaises(broker.BrokerError):
                broker.get("openai-codex")


if __name__ == "__main__":
    unittest.main()
