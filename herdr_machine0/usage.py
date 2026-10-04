"""Subscription usage, fetched once on the hub and shared with every spoke.

Spokes hold no refreshable Claude login (only the setup-token, which the usage
endpoint does not accept), so the hub fetches the plan windows with the
broker's login and caches them; spokes ask through the relay.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.request
from typing import Any, Dict, Optional

from . import broker, config

CLAUDE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_URL = "https://chatgpt.com/backend-api/wham/usage"
PROVIDERS = {"claude": "anthropic", "codex": "openai-codex"}


def _cache(name: str) -> str:
    return config.state_path("usage-%s.json" % name)


def _account_id(token: str) -> Optional[str]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
    except Exception:
        return None


def _fetch(name: str) -> Dict[str, Any]:
    token = broker.access_token(PROVIDERS[name])
    if name == "claude":
        headers = {
            "Authorization": "Bearer " + token,
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
            # Without a claude-code User-Agent this endpoint is heavily rate limited.
            "User-Agent": "claude-code/2.1.0",
        }
        url = CLAUDE_URL
    else:
        headers = {"Authorization": "Bearer " + token, "originator": "pi", "accept": "application/json"}
        account = _account_id(token)
        if account:
            headers["chatgpt-account-id"] = account
        url = CODEX_URL
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


_refreshing: Dict[str, threading.Thread] = {}


def _refresh(name: str) -> None:
    path = _cache(name)
    try:
        data = _fetch(name)
    except Exception:
        return
    tmp = path + ".tmp.%d" % threading.get_ident()
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def get_fast(name: str) -> Dict[str, Any]:
    """For callers with short timeouts (pi's footer over the relay): the cached
    copy at once, refreshed in the background when stale; a blocking fetch
    only when there is no cache at all."""
    if name not in PROVIDERS:
        raise ValueError("unknown usage provider %s" % name)
    path = _cache(name)
    try:
        age = time.time() - os.path.getmtime(path)
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return get(name)
    if age > float(config.settings()["usage_ttl_s"]):
        thread = _refreshing.get(name)
        if thread is None or not thread.is_alive():
            thread = threading.Thread(target=_refresh, args=(name,), daemon=True)
            _refreshing[name] = thread
            thread.start()
    return data


def get(name: str, ttl: Optional[float] = None) -> Dict[str, Any]:
    """Cached usage JSON for claude|codex; serves the last good copy on failure."""
    if name not in PROVIDERS:
        raise ValueError("unknown usage provider %s" % name)
    ttl = float(config.settings()["usage_ttl_s"] if ttl is None else ttl)
    path = _cache(name)
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        age = None
    if age is None or age > ttl:
        try:
            data = _fetch(name)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, path)
            return data
        except Exception:
            if age is None:
                raise
    with open(path) as f:
        return json.load(f)
