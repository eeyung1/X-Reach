import json
import subprocess
from unittest.mock import patch

import pytest

from xreach_radar.cdp_client import ReaClient, ReaCommandError


def test_inspect_web_page_builds_safe_argv_and_decodes_json():
    result = {"normalized_result": {"network": {"requests": []}}}
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(result), stderr=""
    )

    with patch("xreach_radar.cdp_client.subprocess.run", return_value=completed) as run:
        observed = ReaClient(("npx", "rea"), timeout_seconds=4).inspect_web_page(
            "http://127.0.0.1:9222",
            "target-1",
            observation_ms=1500,
            allowed_origins=("https://x.com",),
        )

    assert observed == result
    run.assert_called_once_with(
        (
            "npx",
            "rea",
            "inspect-web-page",
            "http://127.0.0.1:9222",
            "target-1",
            "--observation-ms",
            "1500",
            "--allowed-origins",
            "https://x.com",
            "--include-json-body-shapes",
            "--json",
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=4,
    )


def test_nonzero_exit_preserves_failure_evidence():
    completed = subprocess.CompletedProcess(
        args=[], returncode=7, stdout="", stderr="target unavailable"
    )
    with (
        patch("xreach_radar.cdp_client.subprocess.run", return_value=completed),
        pytest.raises(ReaCommandError) as caught,
    ):
        ReaClient().inspect_web_page("http://127.0.0.1:9222", "target")

    assert caught.value.returncode == 7
    assert caught.value.stderr == "target unavailable"


def test_timeout_is_typed():
    with (
        patch(
            "xreach_radar.cdp_client.subprocess.run",
            side_effect=subprocess.TimeoutExpired(("rea",), 1, stderr=b"waiting"),
        ),
        pytest.raises(ReaCommandError) as caught,
    ):
        ReaClient(timeout_seconds=1).inspect_web_page("http://127.0.0.1:9222", "target")

    assert "timed out" in str(caught.value)
    assert caught.value.stderr == "waiting"


def test_invalid_inputs_never_launch_child():
    with patch("xreach_radar.cdp_client.subprocess.run") as run:
        with pytest.raises(ValueError):
            ReaClient().inspect_web_page("", "target")
        with pytest.raises(ValueError):
            ReaClient().inspect_web_page(
                "http://127.0.0.1:9222", "target", observation_ms=-1
            )
    run.assert_not_called()
