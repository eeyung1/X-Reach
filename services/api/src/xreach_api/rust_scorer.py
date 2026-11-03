"""Build and invoke the Rust scoring bridge over a JSON stdin/stdout protocol."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any


class ScorerUnavailable(RuntimeError):
    """The Rust executable could not be built or could not score the request."""


class RustScorer:
    def __init__(self, binary: str | None = None, timeout_seconds: float = 30) -> None:
        configured = binary or os.environ.get("XREACH_SCORER_BIN")
        self._configured_binary = Path(configured).expanduser() if configured else None
        self._root = Path(__file__).resolve().parents[4]
        self._manifest = self._root / "services/api/rust-bridge/Cargo.toml"
        self._default_binary = (
            self._root
            / "services/api/rust-bridge/target/release/xreach-api-scorer"
        )
        self._timeout_seconds = timeout_seconds
        self._build_lock = threading.Lock()

    def score(
        self,
        candidate: dict[str, Any],
        reach_calibration: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        binary = self._binary()
        request = dict(candidate)
        if reach_calibration is not None:
            request["reach_calibration"] = reach_calibration
        try:
            completed = subprocess.run(
                [str(binary)],
                input=json.dumps(request, allow_nan=False),
                capture_output=True,
                check=False,
                text=True,
                timeout=self._timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ScorerUnavailable("Rust scorer could not be executed") from error
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "Rust scorer returned an error"
            raise ScorerUnavailable(detail)
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ScorerUnavailable("Rust scorer returned invalid JSON") from error
        if not isinstance(result, dict):
            raise ScorerUnavailable("Rust scorer returned an invalid score object")
        return result

    def _binary(self) -> Path:
        if self._configured_binary is not None:
            if not self._configured_binary.is_file():
                raise ScorerUnavailable("XREACH_SCORER_BIN does not point to a file")
            return self._configured_binary
        with self._build_lock:
            if self._default_binary.is_file():
                return self._default_binary
            cargo = shutil.which("cargo")
            if cargo is None:
                raise ScorerUnavailable(
                    "Rust scorer is not built and Cargo is unavailable; build "
                    "services/api/rust-bridge/Cargo.toml first"
                )
            try:
                completed = subprocess.run(
                    [cargo, "build", "--release", "--manifest-path", str(self._manifest)],
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=180,
                    cwd=self._root,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise ScorerUnavailable("Rust scorer build did not complete") from error
            if completed.returncode != 0 or not self._default_binary.is_file():
                detail = completed.stderr.strip() or "Cargo did not produce the scorer binary"
                raise ScorerUnavailable(detail)
            return self._default_binary
