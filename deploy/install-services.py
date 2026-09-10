"""Install a validated fixed release of Mail, its cleaner, and the Beeper gateway."""

import argparse
import datetime
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--ledger-source", type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parent.parent
home = Path.home()
config = home / ".config/apple-mail-fast-mcp"
logs = home / "Library/Logs/apple-mail-fast-mcp"
agents = home / "Library/LaunchAgents"
backup = (
    config / "deployment-backups" / datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%f")
)
backup.mkdir(mode=0o700, parents=True)
logs.mkdir(parents=True, exist_ok=True)
domain = f"gui/{os.getuid()}"


def stop(label):
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)


def start(label):
    p = agents / f"{label}.plist"
    # launchd can briefly retain a booted-out label while its process exits.
    for attempt in range(5):
        result = subprocess.run(["launchctl", "bootstrap", domain, str(p)], capture_output=True)
        if result.returncode == 0:
            break
        if attempt == 4:
            result.check_returncode()
        time.sleep(1)
    subprocess.run(
        ["launchctl", "print", f"{domain}/{label}"], stdout=subprocess.DEVNULL, check=True
    )


def save(label, doc):
    p = agents / f"{label}.plist"
    if p.exists():
        shutil.copy2(p, backup / p.name)
    temp = p.with_suffix(".tmp")
    with temp.open("wb") as f:
        plistlib.dump(doc, f)
    temp.chmod(0o600)
    temp.replace(p)


def job(label, arguments, environment=None):
    return {
        "Label": label,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(root),
        "EnvironmentVariables": environment or {},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(logs / (label + ".out.log")),
        "StandardErrorPath": str(logs / (label + ".err.log")),
    }


# Stop the only existing cleaner before copying its ledger or changing its launch definition.
cleaner = "studio.peacockery.apple-mail-cleaner"
stop(cleaner)
ledger = config / "junk.sqlite"
if not ledger.exists() and args.ledger_source:
    source = sqlite3.connect(f"file:{args.ledger_source}?mode=ro", uri=True)
    destination = sqlite3.connect(ledger)
    try:
        source.backup(destination)
    finally:
        source.close()
        destination.close()
    ledger.chmod(0o600)
mail = "studio.peacockery.apple-mail-mcp"
p = agents / f"{mail}.plist"
environment = plistlib.loads(p.read_bytes()).get("EnvironmentVariables", {}) if p.exists() else {}
environment.update(
    {
        "APPLE_MAIL_MCP_LOCAL_DB": "1",
        "APPLE_MAIL_MCP_APPLESCRIPT_SOCKET": str(config / "applescript-helper.sock"),
    }
)
stop(mail)
save(
    mail,
    job(
        mail,
        [
            str(root / ".venv/bin/apple-mail-mcp"),
            "--transport",
            "http",
            "--listen-host",
            "127.0.0.1",
            "--listen-port",
            "8765",
            "--http-path",
            "/mcp",
            "--bearer-token-file",
            str(config / "http-bearer-token"),
            "--code-mode",
        ],
        environment,
    ),
)
save(
    cleaner,
    job(
        cleaner,
        [
            str(root / ".venv/bin/python"),
            "-m",
            "apple_mail_mcp.junk_automation.cleaner",
            "--interval",
            "30",
        ],
    ),
)
beeper = "studio.peacockery.beeper-mcp"
stop(beeper)
save(beeper, job(beeper, [str(root / ".venv/bin/beeper-gateway")]))
desktop = "studio.peacockery.beeper-desktop"
stop(desktop)
desktop_job = job(
    desktop, [str(root / ".venv/bin/python"), str(root / "deploy/keep-beeper-running.py")]
)
desktop_job.pop("KeepAlive")
desktop_job["StartInterval"] = 60
save(desktop, desktop_job)
for label in [mail, cleaner, beeper, desktop]:
    start(label)
print("Installed services from", root)
print("Previous launch definitions backed up in", backup)
