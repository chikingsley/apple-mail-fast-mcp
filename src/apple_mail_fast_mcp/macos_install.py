"""Install the Apple Mail services on their macOS host."""

from __future__ import annotations

import argparse
import os
import platform
import plistlib
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_NAME = "apple-mail-fast-mcp"
HELPER_LABEL = "studio.peacockery.apple-mail-mcp-helper"
SERVICE_LABEL = "studio.peacockery.apple-mail-mcp"
OPS_LABEL = "studio.peacockery.apple-mail-ops"
OLD_OPS_LABEL = "studio.peacockery.apple-mail-junk-flag-cleaner"
LOCAL_SIGNING_IDENTITY = "Apple Mail MCP Local Signing"
PASSWORD_FILES = {
    "APPLE_MAIL_MCP_IMAP_PASSWORD_FILE_SIMON_PEACOCKERY_STUDIO": "imap-password-peacockery",
    "APPLE_MAIL_MCP_IMAP_PASSWORD_FILE_CHIBUZOR_EJIMOFOR_GMAIL_COM": "imap-password-chibuzor-ejimofor-gmail-com",
    "APPLE_MAIL_MCP_IMAP_PASSWORD_FILE_CHEEZ2012_GMAIL_COM": "imap-password-cheez2012-gmail-com",
    "APPLE_MAIL_MCP_IMAP_PASSWORD_FILE_CHI2137976_MARICOPA_EDU": "imap-password-chi2137976-maricopa-edu",
    "APPLE_MAIL_MCP_IMAP_PASSWORD_FILE_SIMON_EJIMOFOR30_GMAIL_COM": "imap-password-simon-ejimofor30-gmail-com",
    "APPLE_MAIL_MCP_IMAP_PASSWORD_FILE_BLACKKINGRUSSIA_GMAIL_COM": "imap-password-blackkingrussia-gmail-com",
    "APPLE_MAIL_MCP_IMAP_PASSWORD_FILE_HENRY_EBONGA_GMAIL_COM": "imap-password-henry-ebonga-gmail-com",
    "APPLE_MAIL_MCP_IMAP_PASSWORD_FILE_CHIBUZOR_EJIMOFOR_OUTLOOK_COM": "imap-password-chibuzor-ejimofor-outlook-com",
    "APPLE_MAIL_MCP_IMAP_PASSWORD_FILE_CHI_WEIGHANCHOR_COM": "imap-password-chi-weighanchor-com",
}


def _run(
    arguments: list[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        check=check,
        capture_output=capture_output,
        cwd=ROOT,
        env=environment,
        text=True,
    )


def _require_macos() -> None:
    if platform.system() != "Darwin":
        raise RuntimeError("Apple Mail installation requires macOS")


def _directories() -> tuple[Path, Path, Path]:
    home = Path.home()
    config = home / ".config" / CONFIG_NAME
    logs = home / "Library/Logs" / CONFIG_NAME
    agents = home / "Library/LaunchAgents"
    for directory, mode in ((config, 0o700), (logs, 0o700), (agents, 0o700)):
        directory.mkdir(mode=mode, parents=True, exist_ok=True)
        directory.chmod(mode)
    return config, logs, agents


def _validate_secret(path: Path, label: str) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise RuntimeError(f"{label} must be a regular file: {path}")
    if info.st_uid != os.getuid():
        raise RuntimeError(f"{label} must be owned by the current user: {path}")
    if stat.S_IMODE(info.st_mode) not in {0o400, 0o600}:
        raise RuntimeError(f"{label} must use mode 0400 or 0600: {path}")


def _load_plist(path: Path) -> dict[str, object]:
    with path.open("rb") as source:
        value = plistlib.load(source)
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError(f"Invalid property list: {path}")
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _write_plist(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as target:
        plistlib.dump(value, target, sort_keys=False)
    temporary.chmod(0o600)
    temporary.replace(path)
    _run(["plutil", "-lint", str(path)])


def _string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{label} must be a string list")
    return [item for item in value if isinstance(item, str)]


def _string_dict(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise TypeError(f"{label} must be a string dictionary")
    return {
        key: item for key, item in value.items() if isinstance(key, str) and isinstance(item, str)
    }


def _restart(label: str, plist: Path) -> None:
    domain = f"gui/{os.getuid()}"
    _run(["launchctl", "bootout", f"{domain}/{label}"], check=False)
    for attempt in range(3):
        result = _run(["launchctl", "bootstrap", domain, str(plist)], check=False)
        if result.returncode == 0:
            break
        if attempt == 2:
            raise RuntimeError(f"launchd could not bootstrap {label}")
        time.sleep(1)
    _run(["launchctl", "kickstart", "-k", f"{domain}/{label}"])


def _signing_identity_exists() -> bool:
    result = _run(
        ["security", "find-identity", "-v", "-p", "codesigning"],
        check=False,
        capture_output=True,
    )
    return f'"{LOCAL_SIGNING_IDENTITY}"' in result.stdout


def create_signing_identity() -> int:
    """Create or repair the private local code-signing identity."""
    _require_macos()
    if _signing_identity_exists():
        print(f"Code-signing identity exists: {LOCAL_SIGNING_IDENTITY}")
        return 0
    system_keychain = "/Library/Keychains/System.keychain"
    with tempfile.TemporaryDirectory(prefix="apple-mail-signing-") as temporary:
        directory = Path(temporary)
        certificate = directory / "signing.crt"
        found = _run(
            ["security", "find-certificate", "-c", LOCAL_SIGNING_IDENTITY, "-p", system_keychain],
            check=False,
            capture_output=True,
        )
        if found.returncode == 0:
            certificate.write_text(found.stdout, encoding="utf-8")
        else:
            private_key = directory / "signing.key"
            bundle = directory / "signing.p12"
            password = secrets.token_hex(24)
            _run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:3072",
                    "-sha256",
                    "-nodes",
                    "-keyout",
                    str(private_key),
                    "-out",
                    str(certificate),
                    "-days",
                    "3650",
                    "-subj",
                    f"/CN={LOCAL_SIGNING_IDENTITY}/O=Peacockery Studio/OU=Local Development",
                    "-addext",
                    "basicConstraints=critical,CA:TRUE",
                    "-addext",
                    "keyUsage=critical,digitalSignature,keyCertSign",
                    "-addext",
                    "extendedKeyUsage=codeSigning",
                ]
            )
            _run(
                [
                    "openssl",
                    "pkcs12",
                    "-export",
                    "-inkey",
                    str(private_key),
                    "-in",
                    str(certificate),
                    "-out",
                    str(bundle),
                    "-name",
                    LOCAL_SIGNING_IDENTITY,
                    "-passout",
                    f"pass:{password}",
                ]
            )
            _run(
                [
                    "sudo",
                    "security",
                    "import",
                    str(bundle),
                    "-k",
                    system_keychain,
                    "-P",
                    password,
                    "-T",
                    "/usr/bin/codesign",
                    "-T",
                    "/usr/bin/security",
                ]
            )
        _run(
            [
                "sudo",
                "security",
                "add-trusted-cert",
                "-d",
                "-r",
                "trustRoot",
                "-p",
                "codeSign",
                "-k",
                system_keychain,
                str(certificate),
            ]
        )
    if not _signing_identity_exists():
        raise RuntimeError("The local signing identity failed code-signing validation")
    return 0


def install_helper() -> int:
    """Build, sign, install, and start the native AppleScript helper."""
    _require_macos()
    config, logs, agents = _directories()
    home = Path.home()
    source = ROOT / "native/macos-helper"
    app = home / "Applications/Apple Mail MCP Helper.app"
    executable = app / "Contents/MacOS/AppleMailMCPHelper"
    socket_path = config / "applescript-helper.sock"
    target_plist = agents / f"{HELPER_LABEL}.plist"
    identity = os.environ.get("APPLE_MAIL_MCP_CODESIGN_IDENTITY", "")
    if not identity:
        identity = LOCAL_SIGNING_IDENTITY if _signing_identity_exists() else "-"
    with tempfile.TemporaryDirectory(prefix="apple-mail-helper-") as temporary:
        built = Path(temporary) / "AppleMailMCPHelper"
        _run(
            [
                "xcrun",
                "swiftc",
                "-parse-as-library",
                "-O",
                "-framework",
                "AppKit",
                str(source / "AppleMailMCPHelper.swift"),
                "-o",
                str(built),
            ]
        )
        (app / "Contents/MacOS").mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copy2(source / "Info.plist", app / "Contents/Info.plist")
        shutil.copy2(built, executable)
        executable.chmod(0o755)
    _run(
        [
            "codesign",
            "--force",
            "--options",
            "runtime",
            "--sign",
            identity,
            "--entitlements",
            str(source / "AppleMailMCPHelper.entitlements"),
            str(app),
        ]
    )
    _run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])
    _run([str(executable), "--self-check"])
    plist = _load_plist(ROOT / f"deploy/macos/{HELPER_LABEL}.plist")
    plist["ProgramArguments"] = [str(executable), "--serve", str(socket_path)]
    plist["StandardOutPath"] = str(logs / "helper.out.log")
    plist["StandardErrorPath"] = str(logs / "helper.err.log")
    _write_plist(target_plist, plist)
    _restart(HELPER_LABEL, target_plist)
    for _ in range(50):
        if socket_path.exists() and stat.S_ISSOCK(socket_path.stat().st_mode):
            break
        time.sleep(0.1)
    info = socket_path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or socket_path.is_symlink():
        raise RuntimeError(f"Helper socket is unavailable: {socket_path}")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError("Helper socket must be owner-only")
    print(f"Installed {app}")
    return 0


def install_service() -> int:
    """Install the authenticated MCP service and its helper."""
    _require_macos()
    config, logs, agents = _directories()
    if "APPLE_MAIL_MCP_CODESIGN_IDENTITY" not in os.environ:
        create_signing_identity()
    install_helper()
    socket_path = config / "applescript-helper.sock"
    token = config / "http-bearer-token"
    if token.is_symlink():
        raise RuntimeError(f"Bearer token must be a regular file: {token}")
    if not token.exists():
        token.write_text(f"{secrets.token_hex(32)}\n", encoding="utf-8")
        token.chmod(0o600)
    _validate_secret(token, "HTTP bearer token")
    _run(["uv", "sync", "--locked", "--no-dev"], environment=dict(os.environ))
    target_plist = agents / f"{SERVICE_LABEL}.plist"
    plist = _load_plist(ROOT / f"deploy/macos/{SERVICE_LABEL}.plist")
    arguments = _string_list(plist.get("ProgramArguments"), "ProgramArguments")
    arguments[arguments.index("--listen-host") + 1] = os.environ.get(
        "APPLE_MAIL_MCP_LISTEN_HOST", "127.0.0.1"
    )
    arguments[arguments.index("--bearer-token-file") + 1] = str(token)
    plist["ProgramArguments"] = arguments
    plist["WorkingDirectory"] = str(ROOT)
    plist["StandardOutPath"] = str(logs / "service.out.log")
    plist["StandardErrorPath"] = str(logs / "service.err.log")
    environment = _string_dict(plist.get("EnvironmentVariables"), "EnvironmentVariables")
    environment["APPLE_MAIL_MCP_APPLESCRIPT_SOCKET"] = str(socket_path)
    for name, filename in PASSWORD_FILES.items():
        password_file = config / filename
        if password_file.exists():
            _validate_secret(password_file, name)
            environment[name] = str(password_file)
        else:
            environment.pop(name, None)
    plist["EnvironmentVariables"] = environment
    _write_plist(target_plist, plist)
    _restart(SERVICE_LABEL, target_plist)
    helper = (
        Path.home() / "Applications/Apple Mail MCP Helper.app/Contents/MacOS/AppleMailMCPHelper"
    )
    result = _run([str(helper), "--request-mail-automation"], capture_output=True)
    if not result.stdout.strip().isdigit():
        raise RuntimeError("Mail Automation returned an invalid account count")
    print(f"Mail Automation verified: {result.stdout.strip()} accounts")
    return 0


def install_ops() -> int:
    """Install the five-minute junk evidence and deletion supervisor."""
    _require_macos()
    config, logs, agents = _directories()
    state_dir = Path.home() / ".local/state" / CONFIG_NAME
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    socket_path = config / "applescript-helper.sock"
    peacockery_password = config / "imap-password-peacockery"
    if not socket_path.exists() or socket_path.is_symlink():
        raise RuntimeError(f"AppleScript helper socket is unavailable: {socket_path}")
    _validate_secret(peacockery_password, "Peacockery IMAP password")
    target_config = config / "mail-ops.json"
    shutil.copy2(ROOT / "deploy/macos/mail-ops.json", target_config)
    target_config.chmod(0o600)
    old_gog_wrapper = config / "bin/gog"
    if old_gog_wrapper.is_file() and not old_gog_wrapper.is_symlink():
        old_wrapper_text = old_gog_wrapper.read_text(encoding="utf-8")
        if (
            "GOG_KEYRING_PASSWORD" in old_wrapper_text
            and "/opt/homebrew/bin/gog" in old_wrapper_text
        ):
            old_gog_wrapper.unlink()
    target_plist = agents / f"{OPS_LABEL}.plist"
    plist = _load_plist(ROOT / f"deploy/macos/{OPS_LABEL}.plist")
    plist["WorkingDirectory"] = str(ROOT)
    plist["StandardOutPath"] = str(logs / "mail-ops.out.log")
    plist["StandardErrorPath"] = str(logs / "mail-ops.err.log")
    environment = _string_dict(plist.get("EnvironmentVariables"), "EnvironmentVariables")
    environment["APPLE_MAIL_MCP_APPLESCRIPT_SOCKET"] = str(socket_path)
    environment["APPLE_MAIL_MCP_JUNK_CONFIG"] = str(target_config)
    environment["APPLE_MAIL_MCP_OPS_STATUS"] = str(state_dir / "ops-status.json")
    environment["APPLE_MAIL_MCP_IMAP_PASSWORD_FILE_SIMON_PEACOCKERY_STUDIO"] = str(
        peacockery_password
    )
    environment["PATH"] = (
        f"{Path.home()}/.local/bin:{Path.home()}/.opencode/bin:{Path.home()}/.kimi-code/bin:"
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    )
    plist["EnvironmentVariables"] = environment
    _write_plist(target_plist, plist)
    _restart(OPS_LABEL, target_plist)
    old_plist = agents / f"{OLD_OPS_LABEL}.plist"
    _run(["launchctl", "bootout", f"gui/{os.getuid()}/{OLD_OPS_LABEL}"], check=False)
    if old_plist.exists() and not old_plist.is_symlink():
        old_plist.unlink()
    print(f"Installed {OPS_LABEL} on {socket.gethostname()}")
    return 0


def main() -> int:
    """Install one macOS component."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("helper", "ops", "service", "signing-identity"))
    component = parser.parse_args().component
    return {
        "helper": install_helper,
        "ops": install_ops,
        "service": install_service,
        "signing-identity": create_signing_identity,
    }[component]()


if __name__ == "__main__":
    raise SystemExit(main())
