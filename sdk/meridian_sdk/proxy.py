"""meridian-sdk v8.2 — proxy.py
MeridianProxy: transparent wrapper for Anthropic/OpenAI clients.
"""
from __future__ import annotations

import time
import uuid
from typing import Optional

from . import ingest as _ingest
from . import config as _config


class MeridianProxy:
    """
    One-line wrapper for Anthropic or OpenAI clients.

    Args:
        client:     anthropic.Anthropic() or openai.OpenAI() instance
        api_key:    Your Meridian API key (mrd_...). Falls back to MERIDIAN_API_KEY env var.
        feature:    Feature slug, e.g. "ai-chat", "code-review". Shown in Feature Margins.
        user_id:    Your app's user/customer ID. Enables per-user profitability tracking.
        session_id: Optional session identifier for grouping multi-turn conversations.
        ingest_url: Override the Meridian ingest URL (default: https://meridianvisual.io).

    Example (Anthropic):
        import anthropic
        from meridian_sdk import MeridianProxy

        client = MeridianProxy(
            anthropic.Anthropic(),
            api_key="mrd_...",
            feature="ai-chat",
            user_id=request.user.id,
        )
        # Use exactly like anthropic.Anthropic():
        msg = client.messages.create(model="claude-sonnet-4-6", max_tokens=1024, messages=[...])

    Example (OpenAI):
        import openai
        from meridian_sdk import MeridianProxy

        client = MeridianProxy(
            openai.OpenAI(),
            api_key="mrd_...",
            feature="search",
            user_id=current_user.id,
        )
        resp = client.chat.completions.create(model="gpt-4o", messages=[...])

    Per-request overrides:
        # Override user_id or feature for a single call:
        msg = client.messages.create(..., extra_headers={"X-Meridian-User": "usr_abc", "X-Meridian-Feature": "export"})
    """

    def __init__(
        self,
        client,
        api_key: str = "",
        feature: str = "",
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        ingest_url: str = "",
    ):
        self._client    = client
        self._feature   = feature
        self._user_id   = str(user_id) if user_id else None
        self._session_id = str(session_id) if session_id else None

        if api_key:
            _config._config["api_key"] = api_key
        if ingest_url:
            _config._config["ingest_url"] = ingest_url.rstrip("/")

        # Detect provider from client class name
        cname = type(client).__name__.lower()
        if "anthropic" in cname or "async" in cname:
            self._provider = "anthropic"
        elif "openai" in cname:
            self._provider = "openai"
        else:
            self._provider = "unknown"

        self.messages     = _MessagesProxy(self)
        self.chat         = _ChatProxy(self)

    def __getattr__(self, name: str):
        """Pass through any attribute not explicitly wrapped."""
        return getattr(self._client, name)

    def _record(
        self,
        provider: str,
        model: str,
        endpoint: str,
        input_tokens: int,
        output_tokens: int,
        cache_tokens: int,
        latency_ms: int,
        error: Optional[str],
        user_id: Optional[str],
        feature: Optional[str],
        session_id: Optional[str],
    ) -> None:
        _ingest.send({
            "feature":       feature or self._feature or "default",
            "user_id":       user_id or self._user_id,
            "session_id":    session_id or self._session_id,
            "request_id":    str(uuid.uuid4()),
            "provider":      provider or self._provider,
            "model":         model,
            "endpoint":      endpoint,
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
            "cache_tokens":  cache_tokens,
            "latency_ms":    latency_ms,
            "error":         error,
            "metadata":      {},
        })


class _MessagesProxy:
    """Proxies anthropic client.messages.create() and .stream()."""

    def __init__(self, proxy: MeridianProxy):
        self._proxy = proxy

    def create(self, *args, **kwargs):
        # Extract any per-call Meridian overrides from extra_headers
        headers  = kwargs.pop("extra_headers", {}) or {}
        user_id  = headers.pop("X-Meridian-User", None)
        feature  = headers.pop("X-Meridian-Feature", None)
        if headers:
            kwargs["extra_headers"] = headers

        t0 = time.monotonic()
        error = None
        resp  = None
        try:
            resp = self._proxy._client.messages.create(*args, **kwargs)
        except Exception as e:
            error = str(e)[:200]
            raise
        finally:
            latency_ms = int((time.monotonic() - t0) * 1000)
            model  = kwargs.get("model", "")
            it, ot, ct = 0, 0, 0
            if resp is not None:
                u = getattr(resp, "usage", None)
                if u:
                    it = getattr(u, "input_tokens", 0) or 0
                    ot = getattr(u, "output_tokens", 0) or 0
                    ct = (getattr(u, "cache_read_input_tokens", 0) or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
            self._proxy._record(
                provider="anthropic", model=model, endpoint="/messages",
                input_tokens=it, output_tokens=ot, cache_tokens=ct,
                latency_ms=latency_ms, error=error,
                user_id=user_id, feature=feature, session_id=None,
            )
        return resp

    def stream(self, *args, **kwargs):
        """Streaming — wraps the context manager, records usage on close."""
        headers = kwargs.pop("extra_headers", {}) or {}
        user_id = headers.pop("X-Meridian-User", None)
        feature = headers.pop("X-Meridian-Feature", None)
        if headers:
            kwargs["extra_headers"] = headers
        return _AnthropicStreamWrapper(self._proxy, args, kwargs, user_id, feature)

    def __getattr__(self, name):
        return getattr(self._proxy._client.messages, name)


class _AnthropicStreamWrapper:
    def __init__(self, proxy, args, kwargs, user_id, feature):
        self._proxy    = proxy
        self._args     = args
        self._kwargs   = kwargs
        self._user_id  = user_id
        self._feature  = feature
        self._t0       = time.monotonic()
        self._stream   = None

    def __enter__(self):
        self._stream = self._proxy._client.messages.stream(*self._args, **self._kwargs).__enter__()
        return self._stream

    def __exit__(self, *exc_info):
        result = self._stream.__exit__(*exc_info)
        latency_ms = int((time.monotonic() - self._t0) * 1000)
        msg = getattr(self._stream, "get_final_message", lambda: None)()
        it, ot, ct = 0, 0, 0
        if msg:
            u = getattr(msg, "usage", None)
            if u:
                it = getattr(u, "input_tokens", 0) or 0
                ot = getattr(u, "output_tokens", 0) or 0
                ct = (getattr(u, "cache_read_input_tokens", 0) or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        self._proxy._record(
            provider="anthropic", model=self._kwargs.get("model", ""),
            endpoint="/messages/stream", input_tokens=it, output_tokens=ot,
            cache_tokens=ct, latency_ms=latency_ms,
            error=str(exc_info[1])[:200] if exc_info[1] else None,
            user_id=self._user_id, feature=self._feature, session_id=None,
        )
        return result


class _ChatProxy:
    """Proxies openai client.chat.completions.create()."""

    def __init__(self, proxy: MeridianProxy):
        self._proxy = proxy
        self.completions = _CompletionsProxy(proxy)

    def __getattr__(self, name):
        return getattr(self._proxy._client.chat, name)


class _CompletionsProxy:
    def __init__(self, proxy: MeridianProxy):
        self._proxy = proxy

    def create(self, *args, **kwargs):
        user_id = kwargs.pop("meridian_user_id", None)
        feature = kwargs.pop("meridian_feature", None)

        t0    = time.monotonic()
        error = None
        resp  = None
        try:
            resp = self._proxy._client.chat.completions.create(*args, **kwargs)
        except Exception as e:
            error = str(e)[:200]
            raise
        finally:
            latency_ms = int((time.monotonic() - t0) * 1000)
            model  = kwargs.get("model", "")
            it, ot = 0, 0
            if resp is not None:
                u = getattr(resp, "usage", None)
                if u:
                    it = getattr(u, "prompt_tokens", 0) or 0
                    ot = getattr(u, "completion_tokens", 0) or 0
            self._proxy._record(
                provider="openai", model=model, endpoint="/chat/completions",
                input_tokens=it, output_tokens=ot, cache_tokens=0,
                latency_ms=latency_ms, error=error,
                user_id=user_id, feature=feature, session_id=None,
            )
        return resp

    def __getattr__(self, name):
        return getattr(self._proxy._client.chat.completions, name)
