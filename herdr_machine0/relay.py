"""The filtering relay between one spoke slot and the hub's herdr socket.

A spoke's herdr integrations (pi, opencode, claude and codex hooks, the
attention-queue bridge) believe they talk to herdr. They talk to this relay,
reverse-forwarded over the slot's ssh connection. The relay:

- lets through only the reporting methods those integrations use,
- always rewrites the pane to the wrapper's own pane, so a spoke can only ever
  describe itself,
- makes resume after a hub herdr restart run `spoke attach` instead of the
  harness's own resume command, which would start the agent on the hub,
- answers the herdr_machine0.* methods itself (credentials, usage, new slots).
"""

from __future__ import annotations

import json
import os
import socket
import threading
from typing import Any, Callable, Dict, List, Optional

from . import herdr

FORWARDED = frozenset({
    "ping",
    "pane.report_agent",
    "pane.report_agent_session",
    "pane.report_metadata",
    "pane.release_agent",
    "pane.clear_agent_authority",
    "notification.show",
    "plugin.action.invoke",
})
ALLOWED_ACTIONS = frozenset({"attention-queue.refresh"})
REPORTS = frozenset({"pane.report_agent", "pane.report_agent_session"})
# herdr resumes these through a resume_argv the relay injects.
RESUMABLE = {"herdr:pi": "pi", "herdr:opencode": "opencode"}
# herdr would resume these with its built-in command (claude --resume ...) on
# the hub, so their session identity never reaches it; hubd reattaches them.
SESSION_STRIPPED = {"herdr:claude": "claude", "herdr:codex": "codex"}
SESSION_FIELDS = ("agent_session_id", "agent_session_path")

Handler = Callable[[Dict[str, Any]], Dict[str, Any]]


def error(req_id: Any, code: str, message: str) -> Dict[str, Any]:
    return {"id": req_id if isinstance(req_id, str) else "", "error": {"code": code, "message": message}}


class Relay:
    def __init__(
        self,
        herdr_socket: str,
        pane_id: str,
        resume_argv: List[str],
        handlers: Optional[Dict[str, Handler]] = None,
        on_session: Optional[Callable[[str, str, str], None]] = None,
        log: Optional[Callable[[str], None]] = None,
        exchange: Callable[[str, bytes], bytes] = herdr.exchange,
    ):
        self.herdr_socket = herdr_socket
        self.pane_id = pane_id
        self.resume_argv = list(resume_argv)
        self.handlers = handlers or {}
        self.on_session = on_session
        self.log = log or (lambda _msg: None)
        self.exchange = exchange

    # ---- pure request handling -------------------------------------------------

    def rewrite(self, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The request to forward, or None when it should be answered `ok` locally."""
        method = req.get("method")
        params = dict(req.get("params") or {})
        if "pane_id" in params or method in REPORTS or method.startswith("pane."):
            params["pane_id"] = self.pane_id
        if method in REPORTS:
            source = str(params.get("source") or "")
            session = params.get("agent_session_path") or params.get("agent_session_id")
            if source in SESSION_STRIPPED:
                if session and self.on_session:
                    self.on_session(SESSION_STRIPPED[source], "id", str(session))
                if method == "pane.report_agent_session":
                    return None
                for field in SESSION_FIELDS:
                    params.pop(field, None)
                params.pop("resume_argv", None)
            elif source in RESUMABLE:
                if session and self.on_session:
                    kind = "path" if params.get("agent_session_path") else "id"
                    self.on_session(RESUMABLE[source], kind, str(session))
                params["resume_argv"] = list(self.resume_argv)
            else:
                params.pop("resume_argv", None)
        return dict(req, params=params)

    def handle(self, line: bytes) -> bytes:
        try:
            req = json.loads(line.decode())
            if not isinstance(req, dict):
                raise ValueError("not an object")
        except ValueError:
            return json.dumps(error("", "invalid_request", "unreadable request")).encode()
        req_id = req.get("id", "")
        method = str(req.get("method") or "")
        if method in self.handlers:
            try:
                result = self.handlers[method](req.get("params") or {})
                return json.dumps({"id": req_id, "result": result}).encode()
            except Exception as e:  # handler failures are the caller's problem, not ours
                return json.dumps(error(req_id, "handler_failed", str(e))).encode()
        if method not in FORWARDED:
            self.log("deny %s" % method)
            return json.dumps(error(req_id, "method_not_allowed", method)).encode()
        if method == "plugin.action.invoke":
            action = str((req.get("params") or {}).get("action_id") or "")
            if action not in ALLOWED_ACTIONS:
                self.log("deny action %s" % action)
                return json.dumps(error(req_id, "method_not_allowed", "action " + action)).encode()
        forwarded = self.rewrite(req)
        if forwarded is None:
            return json.dumps({"id": req_id, "result": {"type": "ok"}}).encode()
        try:
            reply = self.exchange(self.herdr_socket, json.dumps(forwarded).encode())
        except herdr.Unavailable as e:
            return json.dumps(error(req_id, "herdr_unavailable", str(e))).encode()
        # The session report is applied before herdr checks resume_argv; the
        # rejection only means the reporter does not hold the pane yet.
        try:
            parsed = json.loads(reply.decode())
            if (parsed.get("error") or {}).get("code") == "resume_not_accepted":
                return json.dumps({"id": req_id, "result": {"type": "ok"}}).encode()
        except ValueError:
            pass
        return reply

    # ---- socket server ---------------------------------------------------------

    def serve(self, path: str) -> socket.socket:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o077)
        try:
            server.bind(path)
        finally:
            os.umask(old)
        server.listen(32)
        threading.Thread(target=self._accept, args=(server,), daemon=True).start()
        return server

    def _accept(self, server: socket.socket) -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        try:
            f = conn.makefile("rwb")
            for line in f:
                if not line.strip():
                    continue
                f.write(self.handle(line.rstrip(b"\n")) + b"\n")
                f.flush()
        except OSError:
            pass
        finally:
            conn.close()
