"""Runs `claude -p` (Claude Code CLI) for every reply, using the Claude subscription.

The CLI is started without a shell, fully isolated from user/project configuration,
with no tools, MCP servers, plugins, skills or hooks. The conversation goes through
stdin only and the system prompt through a private temporary file, so neither shows up
in `ps`. An Anthropic API key is never used.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple, Union

from .store import ensure_private_dir

# Builtin plugins reported in the `plugins` field of the stream-json init event
# of Claude Code 2.1.299. All of them are disabled.
BUILTIN_PLUGINS = (
    "cc-plugin-agents-md@builtin",
    "cc-plugin-telemetry@builtin",
    "cc-plugin-plugin-authoring@builtin",
)
ISOLATION_SETTINGS = {
    "disableAllHooks": True,
    "enabledPlugins": {name: False for name in BUILTIN_PLUGINS},
}

_STRIPPED_ENV = {
    "TELEGRAM_BOT_TOKEN",
    "VDL_API_KEY",
    "RELAY_SECRET",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    # Set by --bare; it makes the CLI ignore the OAuth login.
    "CLAUDE_CODE_SIMPLE",
}
_ISOLATION_ENV = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
}

REDACTED = "[REDACTED]"
INSPECT_SYSTEM_PROMPT = "Reply with exactly: OK"
INIT_KEYS = ("tools", "mcp_servers", "plugins", "skills", "apiKeySource", "apiProvider", "model")


class ClaudeError(Exception):
    """A Claude CLI failure whose message is safe to show to the owner."""


class ClaudeTimeout(ClaudeError):
    pass


class ClaudeAuthError(ClaudeError):
    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


class ClaudeLimitError(ClaudeError):
    def __init__(self, detail: str = "", resets_at: Optional[float] = None, reset_text: Optional[str] = None):
        self.detail = detail
        self.resets_at = resets_at
        self.reset_text = reset_text
        if resets_at:
            when = datetime.fromtimestamp(resets_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        else:
            when = reset_text or "time unknown"
        super().__init__(f"Claude subscription usage limit reached (resets {when})")


def build_env(oauth_token: Optional[str] = None, base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    source = os.environ if base is None else base
    env = {
        key: value
        for key, value in source.items()
        if not key.upper().startswith("ANTHROPIC_") and key.upper() not in _STRIPPED_ENV
    }
    env.update(_ISOLATION_ENV)
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return env


_LIMIT_RE = re.compile(
    r"usage limit|hit your (usage )?limit|limit reached|rate[ _-]?limit|out of (extra )?usage|quota",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"not logged in|/login|invalid api key|authentication|unauthori[sz]ed|oauth token", re.IGNORECASE
)
_EPOCH_RE = re.compile(r"\|(\d{9,11})\b")
_RESET_TEXT_RE = re.compile(r"\bresets?\s+(?:at\s+)?([^|\n]{1,80})", re.IGNORECASE)


def classify_failure(message: str, status: Optional[int] = None) -> ClaudeError:
    message = (message or "").strip()
    if status == 429 or _LIMIT_RE.search(message):
        epoch = _EPOCH_RE.search(message)
        resets_at = float(epoch.group(1)) if epoch else None
        detail = _EPOCH_RE.sub("", message).strip()
        text = _RESET_TEXT_RE.search(detail)
        reset_text = text.group(1).strip().rstrip(".") if text else None
        return ClaudeLimitError(detail, resets_at=resets_at, reset_text=reset_text)
    if status in (401, 403) or _AUTH_RE.search(message):
        return ClaudeAuthError(
            "Claude CLI is not logged in or the login was rejected: run `claude` and use /login "
            "(or set CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`)",
            detail=message[:300],
        )
    return ClaudeError(f"Claude CLI failed: {message[:500] or 'unknown error'}")


def _last_json_line(stdout: str) -> Optional[dict]:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
    return None


def parse_result(returncode: Optional[int], stdout: str, stderr: str) -> str:
    data = _last_json_line(stdout)
    if data is None:
        return_text = stderr.strip() or stdout.strip() or f"exit code {returncode}"
        raise classify_failure(return_text)
    result = str(data.get("result") or "")
    if data.get("is_error") or data.get("subtype") != "success":
        status = data.get("api_error_status")
        raise classify_failure(
            " ".join(part for part in (result, stderr.strip()) if part),
            status if isinstance(status, int) else None,
        )
    if not result.strip():
        raise ClaudeError("Claude CLI returned an empty reply")
    return result.strip()


def _kill_tree(proc: "asyncio.subprocess.Process") -> None:
    if hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    else:  # Windows (local development only)
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            proc.kill()
        except ProcessLookupError:
            pass


class ClaudeCLI:
    def __init__(
        self,
        claude_bin: str,
        *,
        workdir: Union[str, Path],
        model: Optional[str] = None,
        effort: str = "low",
        timeout: float = 120.0,
        oauth_token: Optional[str] = None,
        auth_label: Optional[str] = None,
    ):
        self.claude_bin = claude_bin
        self.workdir = Path(workdir)
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self._oauth_token = oauth_token
        self._auth_label = auth_label

    def __repr__(self) -> str:
        return (
            f"ClaudeCLI(claude_bin={self.claude_bin!r}, workdir={str(self.workdir)!r}, model={self.model!r}, "
            f"effort={self.effort!r}, timeout={self.timeout!r}, auth={self.auth_mode!r})"
        )

    @property
    def auth_mode(self) -> str:
        if self._auth_label:
            return self._auth_label
        return "subscription token" if self._oauth_token else "this machine's claude login"

    def build_command(self, system_prompt_file: str, output_format: str = "json") -> List[str]:
        cmd = [
            self.claude_bin, "-p",
            "--safe-mode",
            "--settings", json.dumps(ISOLATION_SETTINGS),
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--system-prompt-file", system_prompt_file,
            "--output-format", output_format,
            "--effort", self.effort,
        ]  # fmt: skip
        if output_format == "stream-json":
            cmd.append("--verbose")
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def _redact(self, text: str) -> str:
        if self._oauth_token:
            text = text.replace(self._oauth_token, REDACTED)
        return text

    async def _run(self, system_prompt: str, user_input: str, output_format: str) -> Tuple[int, str, str]:
        workdir = ensure_private_dir(self.workdir)
        fd, prompt_path = tempfile.mkstemp(prefix="system-", suffix=".txt", dir=workdir)
        try:
            os.chmod(prompt_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(system_prompt)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self.build_command(prompt_path, output_format),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(workdir),
                    env=build_env(self._oauth_token),
                    start_new_session=True,
                )
            except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
                raise ClaudeError(
                    f"claude CLI not found or not executable at {self.claude_bin!r} "
                    f"({type(exc).__name__}). Install Claude Code or set CLAUDE_BIN"
                ) from None
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(user_input.encode("utf-8")), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                _kill_tree(proc)
                await proc.wait()
                raise ClaudeTimeout(f"Claude CLI did not answer within {self.timeout:g} seconds") from None
            except asyncio.CancelledError:
                _kill_tree(proc)
                raise
            return (
                proc.returncode if proc.returncode is not None else -1,
                self._redact(stdout.decode("utf-8", "replace")),
                self._redact(stderr.decode("utf-8", "replace")),
            )
        finally:
            try:
                os.unlink(prompt_path)
            except OSError:
                pass

    def _parse(self, returncode: int, stdout: str, stderr: str) -> str:
        try:
            return parse_result(returncode, stdout, stderr)
        except ClaudeAuthError as exc:
            if not self._oauth_token:
                raise
            raise ClaudeAuthError(
                "Claude rejected the subscription token or it has expired: run `claude setup-token` "
                "on a computer with a browser and update CLAUDE_CODE_OAUTH_TOKEN. "
                f"CLI said: {exc.detail}",
                detail=exc.detail,
            ) from None

    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        returncode, stdout, stderr = await self._run(system_prompt, user_prompt, "json")
        return self._parse(returncode, stdout, stderr)

    async def inspect(self) -> dict:
        returncode, stdout, stderr = await self._run(INSPECT_SYSTEM_PROMPT, "ping", "stream-json")
        init = None
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "system" and event.get("subtype") == "init":
                init = {key: event.get(key) for key in INIT_KEYS}
                break
        reply = self._parse(returncode, stdout, stderr)
        return {"auth": self.auth_mode, "init": init, "reply": reply}
