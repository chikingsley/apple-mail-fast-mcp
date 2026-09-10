"""Start Beeper in the background only when its main executable is absent."""

import plistlib
import subprocess
from pathlib import Path

app = Path("/Applications/Beeper Desktop.app")
info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
executable = str(app / "Contents/MacOS" / info["CFBundleExecutable"])
processes = subprocess.check_output(["ps", "-axo", "comm="], text=True).splitlines()
if executable not in (line.strip() for line in processes):
    subprocess.run(["open", "-gj", "-a", str(app)], check=True)
