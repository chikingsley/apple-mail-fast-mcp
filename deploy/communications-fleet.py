"""List, install, or verify the explicitly registered communications clients.

Run with Python 3.12+ on Hochi. Credentials travel over SSH or SFTP only.
Examples:
  python3 deploy/communications-fleet.py list
  python3 deploy/communications-fleet.py check --all
  python3 deploy/communications-fleet.py install --host glkvm
"""

import argparse
import base64
import io
import json
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path.home() / ".config/peacockery-communications"
HOSTS = json.loads((ROOT / "deploy/communications-fleet.json").read_text())["hosts"]


def ssh(host, command, data=None, timeout=240):
    options = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
    if host.get("port"):
        options += ["-p", str(host["port"])]
    if host.get("identity"):
        options += ["-i", str(Path(host["identity"]).expanduser()), "-o", "IdentitiesOnly=yes"]
    result = subprocess.run(
        [*options, host["ssh"], command],
        input=data,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace")[-1800:])
    return result.stdout.decode(errors="replace")


def powershell(host, command):
    encoded = base64.b64encode(command.encode("utf-16le")).decode()
    return ssh(host, "powershell -NoProfile -EncodedCommand " + encoded)


def transfer(host, relative, data):
    """Use SFTP for Windows data; its SSH console does not reliably deliver stdin."""
    options = ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
    if host.get("port"):
        options += ["-P", str(host["port"])]
    if host.get("identity"):
        options += ["-i", str(Path(host["identity"]).expanduser()), "-o", "IdentitiesOnly=yes"]
    with tempfile.NamedTemporaryFile() as temporary:
        temporary.write(data)
        temporary.flush()
        result = subprocess.run(
            [*options, temporary.name, host["ssh"] + ":.config/peacockery-communications/" + relative],
            capture_output=True, timeout=240,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace")[-1800:])


def payload():
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for relative in [
            "deploy/install-communications.py",
            "deploy/install-communications.py.lock",
            "deploy/communications-client.py",
            "deploy/communications-client.py.lock",
            "deploy/verify-communications.py",
            "skills/apple-mail/SKILL.md",
            "skills/messages/SKILL.md",
        ]:
            archive.add(ROOT / relative, arcname=relative)
        archive.add(ROOT.parent / "apple-calendar-global/skills/apple-calendar/SKILL.md", arcname="skills/apple-calendar/SKILL.md")
        archive.add(ROOT.parent / "apple-contacts-global/skills/apple-contacts/SKILL.md", arcname="skills/apple-contacts/SKILL.md")
    return output.getvalue()


def run(name, host, action):
    if host.get("local"):
        if action == "install":
            subprocess.run(
                [
                    host["uv"],
                    "run",
                    "--python",
                    "3.14.7",
                    "--locked",
                    "--script",
                    str(ROOT / "deploy/install-communications.py"),
                    "--local-service",
                ],
                check=True,
            )
        return subprocess.check_output(
            [host["uv"], "run", "--script", str(ROOT / "deploy/verify-communications.py")],
            text=True,
            timeout=240,
        )
    if not host.get("ssh"):
        raise RuntimeError(host.get("pending", "SSH target is not configured"))
    if host["platform"] == "windows":
        python_arg = (
            " --python '" + host["python"].replace("'", "''") + "'" if host.get("python") else " --python 3.14.7"
        )
        if action == "install":
            powershell(
                host,
                "$ErrorActionPreference='Stop'; $r=Join-Path $env:USERPROFILE '.config/peacockery-communications'; New-Item -ItemType Directory -Force ($r+'/install') | Out-Null; $sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; icacls $r /inheritance:r /grant:r ('*'+$sid+':(OI)(CI)F') | Out-Null; if ($LASTEXITCODE) {exit $LASTEXITCODE}",
            )
            transfer(host, "install.tar.gz", payload())
            powershell(host, "$ErrorActionPreference='Stop'; $r=Join-Path $env:USERPROFILE '.config/peacockery-communications'; tar -xzf ($r+'/install.tar.gz') -C ($r+'/install'); exit $LASTEXITCODE")
            transfer(host, "service-token", (CONFIG / "service-token").read_bytes())
            powershell(
                host,
                "& ($env:USERPROFILE+'/.local/bin/uv.exe') run"
                + python_arg
                + " --locked --script ($env:USERPROFILE+'/.config/peacockery-communications/install/deploy/install-communications.py'); exit $LASTEXITCODE",
            )
        transfer(host, "verify.py", (ROOT / "deploy/verify-communications.py").read_bytes())
        return powershell(
            host,
            "& ($env:USERPROFILE+'/.local/bin/uv.exe') run"
            + python_arg
            + " --script ($env:USERPROFILE+'/.config/peacockery-communications/verify.py'); exit $LASTEXITCODE",
        )
    uv = shlex.quote(host["uv"])
    if action == "install":
        ssh(
            host,
            "umask 077; mkdir -p ~/.config/peacockery-communications/install && chmod 700 ~/.config/peacockery-communications && tar -xzf - -C ~/.config/peacockery-communications/install",
            payload(),
        )
        ssh(
            host,
            "umask 077; cat > ~/.config/peacockery-communications/service-token && chmod 600 ~/.config/peacockery-communications/service-token",
            (CONFIG / "service-token").read_bytes(),
        )
        ssh(
            host,
            uv
            + " run --python 3.14.7 --locked --script ~/.config/peacockery-communications/install/deploy/install-communications.py",
        )
    ssh(
        host,
        "umask 077; cat > ~/.config/peacockery-communications/verify.py",
        (ROOT / "deploy/verify-communications.py").read_bytes(),
    )
    return ssh(host, uv + " run --script ~/.config/peacockery-communications/verify.py")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["list", "check", "install"])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--host", choices=list(HOSTS))
    group.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if args.action == "list":
        print(json.dumps(HOSTS, indent=2))
        return
    if not args.host and not args.all:
        parser.error("choose --host NAME or --all")
    failed = False
    for name in [args.host] if args.host else HOSTS:
        try:
            print(name + ": " + run(name, HOSTS[name], args.action), flush=True)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            failed = True
            print(name + ": FAILED: " + str(error), flush=True)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
