# meridian-sdk v8.3

One-line wrapper that intercepts every LLM call and sends cost + token data to your Meridian workspace. Zero dependencies. Zero latency added (fire-and-forget background thread).

## Install

```bash
pip install meridian-sdk
```

## Anthropic — two line change

```python
import anthropic
from meridian_sdk import MeridianProxy

# Before:
# client = anthropic.Anthropic()

# After — one line change:
client = MeridianProxy(anthropic.Anthropic(), api_key="mrd_...", feature="ai-chat", user_id=request.user.id)

# Everything else stays identical
msg = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}]
)
```

## OpenAI — same pattern

```python
import openai
from meridian_sdk import MeridianProxy

client = MeridianProxy(openai.OpenAI(), api_key="mrd_...", feature="search", user_id=user.id)

resp = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Hello"}]
)
```

## Streaming (Anthropic)

```python
with client.messages.stream(model="claude-sonnet-4-6", max_tokens=1024, messages=[...]) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
# Usage is recorded automatically when the stream closes
```

## Per-request overrides

```python
# Override user_id or feature for a single call:
msg = client.messages.create(
    ...,
    extra_headers={"X-Meridian-User": "usr_premium_456", "X-Meridian-Feature": "export"}
)
```

## Environment variables

| Variable | Description |
|---|---|
| `MERIDIAN_API_KEY` | Your Meridian API key (fallback if not passed to constructor) |
| `MERIDIAN_INGEST_URL` | Override ingest URL (default: `https://meridianvisual.io`) |
| `MERIDIAN_DEBUG` | Set to `1` to print ingest logs to stdout |

## Global config

```python
import meridian_sdk
meridian_sdk.configure(api_key="mrd_...", debug=True)
```
