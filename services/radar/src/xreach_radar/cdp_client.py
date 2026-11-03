"""Safe subprocess adapter for REA browser observation commands."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReaCommandError(RuntimeError):
    """A typed failure from invoking or decoding the REA CLI."""

    message: str
    command: tuple[str, ...]
    returncode: int | None = None
    stderr: str = ""

    def __str__(self) -> str:
        detail = f" (exit {self.returncode})" if self.returncode is not None else ""
        return f"{self.message}{detail}: {' '.join(self.command)}"


class ReaClient:
    """Invoke REA as an argv-only child process and return its JSON envelope."""

    def __init__(
        self,
        command: Sequence[str] = ("rea",),
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not command or any(not part for part in command):
            raise ValueError("command must contain at least one non-empty argv item")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._command = tuple(command)
        self._timeout_seconds = timeout_seconds

    def inspect_web_page(
        self,
        cdp_endpoint: str,
        target_id: str,
        *,
        observation_ms: int = 1_000,
        allowed_origins: Sequence[str] = (),
        include_json_body_shapes: bool = True,
    ) -> Mapping[str, Any]:
        """Passively inspect one selected browser target through REA.

        REA returns metadata and, when requested, value-free JSON body shapes.
        This method does not claim that response body values were captured.
        """

        if not cdp_endpoint:
            raise ValueError("cdp_endpoint must not be empty")
        if not target_id:
            raise ValueError("target_id must not be empty")
        if observation_ms < 0:
            raise ValueError("observation_ms must be non-negative")

        arguments = [
            "inspect-web-page",
            cdp_endpoint,
            target_id,
            "--observation-ms",
            str(observation_ms),
        ]
        for origin in allowed_origins:
            if not origin:
                raise ValueError("allowed_origins must not contain empty values")
            arguments.extend(("--allowed-origins", origin))
        if include_json_body_shapes:
            arguments.append("--include-json-body-shapes")
        arguments.append("--json")
        return self._run_json(arguments)

    def _run_json(self, arguments: Sequence[str]) -> Mapping[str, Any]:
        command = (*self._command, *arguments)
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise ReaCommandError(
                "REA command timed out", command, stderr=_as_text(error.stderr)
            ) from error
        except OSError as error:
            raise ReaCommandError(
                f"REA command could not start: {error}", command
            ) from error

        if completed.returncode != 0:
            raise ReaCommandError(
                "REA command failed",
                command,
                returncode=completed.returncode,
                stderr=completed.stderr.strip(),
            )
        try:
            decoded = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ReaCommandError(
                f"REA returned invalid JSON at character {error.pos}",
                command,
                returncode=completed.returncode,
                stderr=completed.stderr.strip(),
            ) from error
        if not isinstance(decoded, dict):
            raise ReaCommandError(
                "REA JSON result must be an object",
                command,
                returncode=completed.returncode,
                stderr=completed.stderr.strip(),
            )
        return decoded


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value
