"""Settings loading.

Settings come from exactly one source:
  * local: the `.env` file only (read with `dotenv_values`, the shell environment is ignored);
  * container: a mapping built by `bot.py --settings-from-env` from the process environment.

Nothing is ever written to `os.environ`, so secrets never leak into other processes.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Union

from dotenv import dotenv_values

PROJECT_DIR = Path(__file__).resolve().parent.parent

SETTING_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "OWNER_ID",
    "AUTO_SEND",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_BIN",
    "CLAUDE_MODEL",
    "CLAUDE_EFFORT",
    "CLAUDE_TIMEOUT_SECONDS",
    "LIMIT_COOLDOWN_MINUTES",
    "OWNER_TAKEOVER_MINUTES",
    "HISTORY_LIMIT",
    "DATA_DIR",
    "PROFILE_PATH",
    "VDL_API_KEY",
    "VDL_API_URL",
    "AGENT_MODE",
    "RELAY_SECRET",
    "PORT",
)

AGENT_MODES = ("business", "relay")
MIN_RELAY_SECRET_LENGTH = 24

DEFAULT_VDL_API_URL = "https://vdl-market.lovable.app/api/public/agent/search"

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}
_BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")

AUTH_LOCAL_LOGIN = "this machine's claude login"
AUTH_TOKEN_DOTENV = "subscription token from .env"
AUTH_TOKEN_ENVIRONMENT = "subscription token from environment variables"


class ConfigError(Exception):
    """Invalid or missing configuration. Messages never contain secret values."""


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    owner_id: int
    auto_send: bool
    claude_oauth_token: Optional[str] = field(repr=False)
    claude_bin: str
    claude_model: Optional[str]
    claude_effort: str
    claude_timeout: float
    limit_cooldown_minutes: int
    owner_takeover_minutes: int
    history_limit: int
    data_dir: Path
    profile_path: Path
    vdl_api_key: Optional[str] = field(default=None, repr=False)
    vdl_api_url: str = DEFAULT_VDL_API_URL
    agent_mode: str = "business"
    relay_secret: Optional[str] = field(default=None, repr=False)
    port: int = 8080
    from_environment: bool = False

    @property
    def relay_mode(self) -> bool:
        return self.agent_mode == "relay"

    @property
    def vdl_search_enabled(self) -> bool:
        return bool(self.vdl_api_key)

    @property
    def claude_auth_mode(self) -> str:
        if not self.claude_oauth_token:
            return AUTH_LOCAL_LOGIN
        return AUTH_TOKEN_ENVIRONMENT if self.from_environment else AUTH_TOKEN_DOTENV

    @property
    def claude_workdir(self) -> Path:
        return self.data_dir / "claude-run"


def default_claude_bin() -> str:
    found = shutil.which("claude")
    if found:
        return found
    local_bin = Path.home() / ".local" / "bin"
    for name in ("claude", "claude.exe"):
        candidate = local_bin / name
        if candidate.exists():
            return str(candidate)
    return "claude"


def load_settings(
    env_file: Union[str, Path] = PROJECT_DIR / ".env",
    *,
    require_telegram: bool = True,
    overrides: Optional[Mapping[str, str]] = None,
    source: Optional[Mapping[str, str]] = None,
) -> Settings:
    env_path = Path(env_file)
    base_dir = env_path.resolve().parent

    if source is not None:
        where = "the environment variables"
        raw = {k: v for k, v in source.items() if v is not None}
    else:
        where = str(env_path)
        if not env_path.exists():
            raise ConfigError(
                f"{env_path} not found. Create it with: cp .env.example .env && chmod 600 .env"
            )
        raw = {k: v for k, v in dotenv_values(env_path).items() if v is not None}
    if overrides:
        raw.update(overrides)

    def get(key: str) -> str:
        return str(raw.get(key, "") or "").strip()

    def fail(message: str) -> ConfigError:
        return ConfigError(f"{message} (check {where})")

    def integer(key: str, default: int, minimum: int) -> int:
        value = get(key)
        if not value:
            return default
        try:
            number = int(value)
        except ValueError:
            raise fail(f"{key} must be a whole number") from None
        if number < minimum:
            raise fail(f"{key} must be at least {minimum}")
        return number

    bot_token = get("TELEGRAM_BOT_TOKEN")
    owner_raw = get("OWNER_ID")
    owner_id = 0
    if require_telegram:
        if not bot_token:
            raise fail("TELEGRAM_BOT_TOKEN is missing")
        if not _BOT_TOKEN_RE.match(bot_token):
            raise fail("TELEGRAM_BOT_TOKEN does not look like a bot token (<digits>:<text>)")
        if not owner_raw:
            raise fail("OWNER_ID is missing")
    if owner_raw:
        if not owner_raw.isdigit():
            raise fail("OWNER_ID must contain digits only")
        owner_id = int(owner_raw)

    auto_raw = get("AUTO_SEND").lower() or "false"
    if auto_raw in _TRUE:
        auto_send = True
    elif auto_raw in _FALSE:
        auto_send = False
    else:
        raise fail("AUTO_SEND must be one of true/false/1/0/yes/no/on/off")

    oauth = get("CLAUDE_CODE_OAUTH_TOKEN") or None
    if oauth:
        if oauth.startswith("sk-ant-api"):
            raise fail(
                "CLAUDE_CODE_OAUTH_TOKEN is an Anthropic API key, not a subscription token. "
                "Run `claude setup-token` and use the sk-ant-oat... token it prints"
            )
        if not oauth.startswith("sk-ant-oat") or re.search(r"\s", oauth):
            raise fail(
                "CLAUDE_CODE_OAUTH_TOKEN is not a valid `claude setup-token` token "
                "(it must start with sk-ant-oat and be on a single line without spaces)"
            )

    effort = get("CLAUDE_EFFORT").lower() or "low"
    if effort not in EFFORT_LEVELS:
        raise fail(f"CLAUDE_EFFORT must be one of {', '.join(EFFORT_LEVELS)}")

    vdl_key = get("VDL_API_KEY") or None
    if vdl_key and re.search(r"\s", vdl_key):
        raise fail("VDL_API_KEY must be on a single line without spaces")
    vdl_url = get("VDL_API_URL") or DEFAULT_VDL_API_URL
    if not vdl_url.startswith("https://"):
        raise fail("VDL_API_URL must start with https://")

    agent_mode = get("AGENT_MODE").lower() or "business"
    if agent_mode not in AGENT_MODES:
        raise fail(f"AGENT_MODE must be one of {', '.join(AGENT_MODES)}")
    relay_secret = get("RELAY_SECRET") or None
    if relay_secret and (len(relay_secret) < MIN_RELAY_SECRET_LENGTH or re.search(r"\s", relay_secret)):
        raise fail(f"RELAY_SECRET must be at least {MIN_RELAY_SECRET_LENGTH} characters without spaces")
    if agent_mode == "relay" and require_telegram and not relay_secret:
        raise fail("RELAY_SECRET is required when AGENT_MODE=relay")
    port = integer("PORT", 8080, 1)
    if port > 65535:
        raise fail("PORT must be at most 65535")

    def path(key: str, default: str) -> Path:
        value = Path(os.path.expanduser(get(key) or default))
        return value if value.is_absolute() else base_dir / value

    claude_bin = get("CLAUDE_BIN")
    claude_bin = os.path.expanduser(claude_bin) if claude_bin else default_claude_bin()

    return Settings(
        bot_token=bot_token,
        owner_id=owner_id,
        auto_send=auto_send,
        claude_oauth_token=oauth,
        claude_bin=claude_bin,
        claude_model=get("CLAUDE_MODEL") or None,
        claude_effort=effort,
        claude_timeout=float(integer("CLAUDE_TIMEOUT_SECONDS", 120, 1)),
        limit_cooldown_minutes=integer("LIMIT_COOLDOWN_MINUTES", 30, 1),
        owner_takeover_minutes=integer("OWNER_TAKEOVER_MINUTES", 15, 0),
        history_limit=integer("HISTORY_LIMIT", 30, 1),
        data_dir=path("DATA_DIR", "data"),
        profile_path=path("PROFILE_PATH", "profile.md"),
        vdl_api_key=vdl_key,
        vdl_api_url=vdl_url,
        agent_mode=agent_mode,
        relay_secret=relay_secret,
        port=port,
        from_environment=source is not None,
    )


def env_file_warning(path: Union[str, Path]) -> Optional[str]:
    """Return a warning when the .env file is readable by group or others."""
    if os.name != "posix":
        return None
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return (
            f"{path} is accessible by other users (mode {oct(mode & 0o777)}). "
            f"Restrict it with: chmod 600 {path}"
        )
    return None
