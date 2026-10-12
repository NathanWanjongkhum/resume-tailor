"""Look up a secret without ever storing it in the repo.

Order: environment variable, systemd credential (for the timer service),
then the desktop keyring (KWallet / GNOME Keyring via secret-tool).

Store one in the keyring with:
    secret-tool store --label="resume-tailor <name>" service resume-tailor account <name>
"""

import os
import shutil
import subprocess
from pathlib import Path


def get(name: str) -> str | None:
    env = name.upper().replace("-", "_")
    if value := os.environ.get(env, ""):
        return value
    if creds := os.environ.get("CREDENTIALS_DIRECTORY", ""):
        path = Path(creds) / name
        if path.exists():
            return path.read_text().strip()
    if shutil.which("secret-tool") is not None:
        out = subprocess.run(
            ["secret-tool", "lookup", "service", "resume-tailor", "account", name],
            check=False,
            capture_output=True,
            text=True,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return None


def require(name: str, how: str = "") -> str:
    value = get(name)
    if value is None:
        raise RuntimeError(
            f"secret {name!r} not found. Store it with:\n"
            f"  secret-tool store --label='resume-tailor {name}' service resume-tailor account {name}"
            + (f"\n{how}" if how else "")
        )
    return value
