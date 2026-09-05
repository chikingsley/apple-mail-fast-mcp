"""Keep the user-authorized shared Beeper Desktop service available."""

import subprocess

if subprocess.run(["pgrep", "-x", "Beeper"], capture_output=True).returncode:
    subprocess.run(["open", "-gj", "-a", "/Applications/Beeper Desktop.app"], check=True)
