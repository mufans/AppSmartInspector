"""Global runtime configuration for SmartInspector.

Stores mutable settings that can be set via CLI args or /config command.
LLM model configuration via environment variables or .env file.
"""

import os

from dotenv import load_dotenv

# Load .env file from project root
load_dotenv()

_DEFAULT_WS_PORT = 9876

_source_dir: str = "."

# ── LLM Model Configuration ──────────────────────────────────
#
# Environment variables:
#   SI_MODEL          — default model for all roles (default: deepseek-chat)
#   SI_BASE_URL       — OpenAI-compatible API base URL (default: https://api.deepseek.com)
#   SI_API_KEY        — API key (falls back to OPENAI_API_KEY)
#   SI_ATTRIBUTOR_MODEL — model override for attributor (code understanding)
#
# Each role can be overridden individually; falls back to SI_MODEL.

_DEFAULT_MODEL = "deepseek-chat"
_DEFAULT_BASE_URL = "https://api.deepseek.com"


def get_model(role: str = "default") -> str:
    """Get model name for a given role.

    Role-specific overrides:
      attributor → SI_ATTRIBUTOR_MODEL
      (others)   → SI_MODEL
    """
    # Role-specific override
    env_key = f"SI_{role.upper()}_MODEL"
    override = os.environ.get(env_key)
    if override:
        return override
    return os.environ.get("SI_MODEL", _DEFAULT_MODEL)


def get_base_url() -> str:
    """Get API base URL."""
    return os.environ.get("SI_BASE_URL", _DEFAULT_BASE_URL)


def get_api_key() -> str:
    """Get API key.

    Priority: SI_API_KEY > OPENAI_API_KEY > empty string.
    """
    return os.environ.get("SI_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")


def get_llm_kwargs(**overrides) -> dict:
    """Get common kwargs for ChatOpenAI construction.

    Returns a dict suitable for ChatOpenAI(**kwargs).
    Caller can add temperature, streaming, etc. via overrides.
    """
    kwargs = {
        "model": get_model(overrides.pop("role", "default")),
        "base_url": get_base_url(),
    }
    api_key = get_api_key()
    if api_key:
        kwargs["api_key"] = api_key
    kwargs.update(overrides)
    return kwargs


def model_info() -> str:
    """Return a human-readable summary of current model config."""
    lines = [
        f"Model: {get_model()}",
        f"Base URL: {get_base_url()}",
        f"API Key: {'set' if get_api_key() else 'not set'}",
    ]
    attributor_model = get_model("attributor")
    if attributor_model != get_model():
        lines.append(f"Attributor: {attributor_model}")
    return "\n".join(lines)


# ── Source directory ──────────────────────────────────────────


def get_ws_port() -> int:
    """Get WebSocket server port.

    Priority: SI_WS_PORT env var > default (9876).
    """
    try:
        return int(os.environ.get("SI_WS_PORT", _DEFAULT_WS_PORT))
    except (ValueError, TypeError):
        return _DEFAULT_WS_PORT


def get_source_dir() -> str:
    """Get the current source code search directory."""
    return _source_dir


def set_source_dir(path: str) -> None:
    """Set the source code search directory.

    Expands ~ and resolves to absolute path.
    """
    global _source_dir
    expanded = os.path.expanduser(path)
    _source_dir = os.path.abspath(expanded)
