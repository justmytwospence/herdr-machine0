import json
import os
import socket
import tempfile
import unittest

from herdr_machine0 import relay


class FakeHerdr:
    def __init__(self, reply=None):
        self.sent = []
        self.reply = reply or {"result": {"type": "ok"}}

    def __call__(self, path, line):
        req = json.loads(line.decode())
        self.sent.append(req)
        return json.dumps(dict(self.reply, id=req.get("id"))).encode()


def make(reply=None, handlers=None):
    fake = FakeHerdr(reply)
    sessions = []
    r = relay.Relay("/x.sock", "w1:p3", ["spoke", "attach", "demo", "main", "--harness", "pi"],
                    handlers=handlers, on_session=lambda *a: sessions.append(a), exchange=fake)
    return r, fake, sessions


def call(r, method, params=None, id="1"):
    return json.loads(r.handle(json.dumps({"id": id, "method": method, "params": params or {}}).encode()))


class RelayTest(unittest.TestCase):
    def test_denies_everything_not_reporting(self):
        r, fake, _ = make()
        for method in ("pane.list", "pane.send_text", "pane.read", "agent.prompt", "server.stop", "plugin.link"):
            reply = call(r, method, {"pane_id": "w1:p1"})
            self.assertEqual(reply["error"]["code"], "method_not_allowed", method)
        self.assertEqual(fake.sent, [])

    def test_ping_and_notification_forwarded(self):
        r, fake, _ = make()
        call(r, "ping")
        call(r, "notification.show", {"title": "x"})
        self.assertEqual([s["method"] for s in fake.sent], ["ping", "notification.show"])

    def test_pane_is_always_rewritten(self):
        r, fake, _ = make()
        call(r, "pane.report_metadata", {"pane_id": "w9:p9", "source": "user:x", "tokens": {"a": "b"}})
        call(r, "pane.report_agent", {"source": "custom", "agent": "x", "state": "idle"})
        self.assertEqual([s["params"]["pane_id"] for s in fake.sent], ["w1:p3", "w1:p3"])

    def test_plugin_actions_limited(self):
        r, fake, _ = make()
        bad = call(r, "plugin.action.invoke", {"action_id": "machine0.suspend-focused"})
        self.assertEqual(bad["error"]["code"], "method_not_allowed")
        call(r, "plugin.action.invoke", {"action_id": "attention-queue.refresh"})
        self.assertEqual(len(fake.sent), 1)

    def test_pi_reports_get_resume_argv(self):
        r, fake, sessions = make()
        call(r, "pane.report_agent_session", {"pane_id": "w1:p1", "source": "herdr:pi", "agent": "pi",
                                              "agent_session_path": "/home/ubuntu/s.jsonl", "seq": 5})
        sent = fake.sent[0]["params"]
        self.assertEqual(sent["resume_argv"], ["spoke", "attach", "demo", "main", "--harness", "pi"])
        self.assertEqual(sent["agent_session_path"], "/home/ubuntu/s.jsonl")
        self.assertEqual(sessions, [("pi", "path", "/home/ubuntu/s.jsonl")])

    def test_custom_sources_cannot_set_resume(self):
        r, fake, _ = make()
        call(r, "pane.report_agent", {"source": "custom:x", "agent": "x", "state": "idle", "resume_argv": ["rm", "-rf"]})
        self.assertNotIn("resume_argv", fake.sent[0]["params"])

    def test_claude_session_never_reaches_herdr(self):
        r, fake, sessions = make()
        reply = call(r, "pane.report_agent_session", {"source": "herdr:claude", "agent": "claude",
                                                      "agent_session_id": "abc"})
        self.assertEqual(reply["result"]["type"], "ok")
        self.assertEqual(fake.sent, [])
        call(r, "pane.report_agent", {"source": "herdr:codex", "agent": "codex", "state": "working",
                                      "agent_session_id": "zzz"})
        self.assertNotIn("agent_session_id", fake.sent[0]["params"])
        self.assertEqual(sessions, [("claude", "id", "abc"), ("codex", "id", "zzz")])

    def test_resume_not_accepted_reads_as_ok(self):
        r, _, _ = make(reply={"error": {"code": "resume_not_accepted", "message": "hold the pane"}})
        reply = call(r, "pane.report_agent_session", {"source": "herdr:pi", "agent": "pi", "agent_session_id": "s"})
        self.assertEqual(reply["result"]["type"], "ok")

    def test_local_handlers(self):
        r, fake, _ = make(handlers={"herdr_machine0.credential": lambda p: {"access": "a-" + p["provider"]}})
        reply = call(r, "herdr_machine0.credential", {"provider": "radius"})
        self.assertEqual(reply["result"]["access"], "a-radius")
        self.assertEqual(fake.sent, [])

    def test_handler_failure_is_an_error_reply(self):
        def boom(_p):
            raise ValueError("no")
        r, _, _ = make(handlers={"herdr_machine0.credential": boom})
        self.assertEqual(call(r, "herdr_machine0.credential")["error"]["code"], "handler_failed")

    def test_garbage(self):
        r, _, _ = make()
        self.assertEqual(json.loads(r.handle(b"not json"))["error"]["code"], "invalid_request")

    def test_socket_server_roundtrip(self):
        r, fake, _ = make()
        path = os.path.join(tempfile.mkdtemp(), "relay.sock")
        server = r.serve(path)
        try:
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o700")
            s = socket.socket(socket.AF_UNIX)
            s.connect(path)
            f = s.makefile("rwb")
            for i in range(2):
                f.write(json.dumps({"id": str(i), "method": "ping", "params": {}}).encode() + b"\n")
                f.flush()
                self.assertEqual(json.loads(f.readline())["id"], str(i))
            s.close()
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
