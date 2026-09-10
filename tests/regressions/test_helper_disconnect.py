"""Regression: September 10 draft inspection timeouts killed the helper with SIGPIPE."""

import json
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from apple_mail_fast_mcp.exceptions import MailAppleScriptError
from apple_mail_fast_mcp.mail_connector import AppleMailConnector


@pytest.mark.allow_real_io
@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS helper regression")
def test_regression_helper_survives_disconnected_reader():
    """Regression: one timed-out client must not kill other clients' shared helper.

    Compile the actual native source and use an isolated socket; no Mail events,
    user messages, production helper, or Accessibility prompts are involved.
    """
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="mh-", dir="/tmp") as temporary:
        binary = Path(temporary) / "helper"
        socket_path = Path(temporary) / "helper.sock"
        subprocess.run(
            [
                "/usr/bin/xcrun",
                "swiftc",
                "-parse-as-library",
                "-O",
                "-framework",
                "AppKit",
                str(root / "native/macos-helper/AppleMailMCPHelper.swift"),
                str(root / "native/macos-helper/MailIndex.swift"),
                str(root / "native/macos-helper/NativeComposition.swift"),
                "-lsqlite3",
                "-o",
                str(binary),
            ],
            check=True,
            capture_output=True,
            timeout=90,
        )
        with subprocess.Popen([str(binary), "--serve", str(socket_path)]) as process:
            try:
                deadline = time.monotonic() + 5
                while not socket_path.exists() and time.monotonic() < deadline:
                    assert process.poll() is None
                    time.sleep(0.02)
                assert socket_path.exists()
                connector = AppleMailConnector(timeout=1)
                connector._applescript_socket = socket_path
                with pytest.raises(MailAppleScriptError, match="timeout"):
                    connector._run_applescript('delay 2\nreturn "late response"')
                connector.timeout = 5
                assert connector._run_applescript('return "after-disconnect"') == "after-disconnect"
                assert process.poll() is None
                process.send_signal(signal.SIGPIPE)
                assert connector._run_applescript('return "still-alive"') == "still-alive"
                capabilities = json.loads(connector._run_applescript("ACCESSIBILITY\n"))
                assert isinstance(capabilities["trusted"], bool)
                assert capabilities["pid"] == process.pid
                with pytest.raises(MailAppleScriptError, match="structured result"):
                    connector._run_applescript("return {1, 2}")
            finally:
                process.terminate()
                process.wait(timeout=5)
