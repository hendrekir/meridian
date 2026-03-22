"""
meridian-sdk v8.1
One-line wrapper that intercepts every LLM call and sends cost + token data
to your Meridian workspace in the background. Zero latency added.

Usage:
    from meridian_sdk import MeridianProxy
    client = MeridianProxy(anthropic.Anthropic(), api_key="mrd_...", feature="ai-chat")
    # Use exactly like anthropic.Anthropic() — nothing else changes.
"""

from .proxy import MeridianProxy
from .config import configure

__all__ = ["MeridianProxy", "configure"]
__version__ = "8.1.0"
