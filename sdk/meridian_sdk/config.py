"""meridian-sdk v8.1 — config.py
Global configuration singleton. Set once at startup via configure().
"""
import os

_config = {
    "api_key":    os.getenv("MERIDIAN_API_KEY", ""),
    "ingest_url": os.getenv("MERIDIAN_INGEST_URL", "https://meridianvisual.io"),
    "debug":      os.getenv("MERIDIAN_DEBUG", "").lower() in ("1", "true"),
    "timeout":    float(os.getenv("MERIDIAN_TIMEOUT", "4")),
}


def configure(
    api_key: str = "",
    ingest_url: str = "",
    debug: bool = False,
    timeout: float = 4.0,
):
    """
    Configure SDK-wide defaults. Call once at app startup.
    All arguments are optional — unset values fall through to env vars.

    Example:
        import meridian_sdk
        meridian_sdk.configure(api_key="mrd_...", ingest_url="https://meridianvisual.io")
    """
    if api_key:
        _config["api_key"] = api_key
    if ingest_url:
        _config["ingest_url"] = ingest_url.rstrip("/")
    if debug:
        _config["debug"] = debug
    if timeout != 4.0:
        _config["timeout"] = timeout


def get(key: str):
    return _config.get(key)
