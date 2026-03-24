"""
meridian-sdk v8.3 - ingest.py
Fire-and-forget background HTTP sender to Meridian proxy ingest endpoint.
"""
import threading
import time
import urllib.request
import urllib.error
import json

from . import config


def send(payload: dict) -> None:
    """Send a transaction payload in a daemon thread. Never blocks, never raises."""
    t = threading.Thread(target=_send, args=(payload,), daemon=True)
    t.start()


def _send(payload: dict) -> None:
    api_key = config.get("api_key")
    if not api_key:
        if config.get("debug"):
            print(f"[meridian] MERIDIAN_API_KEY not set - skipping ingest: {payload}")
        return

    url = config.get("ingest_url") + "/proxy/ingest"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Meridian-Key": api_key,
        },
        method="POST",
    )
    try:
        timeout = config.get("timeout") or 4.0
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if config.get("debug"):
                body = resp.read().decode()
                print(f"[meridian] ingest ok: {body}")
    except urllib.error.HTTPError as e:
        if config.get("debug"):
            print(f"[meridian] ingest HTTP {e.code}: {e.read().decode()[:200]}")
    except Exception as e:
        if config.get("debug"):
            print(f"[meridian] ingest error: {e}")
