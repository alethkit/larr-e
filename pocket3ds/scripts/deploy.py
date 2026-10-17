"""Install LARR-E on a 3DS running ftpd (port 5000): the pairing key, then the .3dsx.

    python3 scripts/deploy.py <3ds-ip>

Creates .local/pair.key on first run. The bridge reads the same key; the 3DS reads it from
sdmc:/pocketjs/offload/<slot>.key, where slot = first 16 hex of sha256(pocket.json "id").
"""

import ftplib
import hashlib
import io
import json
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BINARY = ROOT / "dist" / "larr-e-main.3dsx"
KEY = ROOT / ".local" / "pair.key"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    address = sys.argv[1]
    if not BINARY.exists():
        sys.exit(f"{BINARY} not found: run `bun run 3ds` first")
    data = BINARY.read_bytes()
    if b"pocketjs-captures" in data:
        sys.exit("Refusing to deploy a capture build; rebuild without --capture")
    if not KEY.exists():
        KEY.parent.mkdir(exist_ok=True)
        KEY.write_text(secrets.token_hex(32))
    os.chmod(KEY, 0o600)

    manifest_id = json.loads((ROOT / "pocket.json").read_text())["id"]
    slot = hashlib.sha256(manifest_id.encode()).hexdigest()[:16]

    ftp = ftplib.FTP()
    ftp.connect(address, 5000, timeout=20)
    ftp.login()
    for path in ["/pocketjs", "/pocketjs/offload", "/3ds"]:
        try:
            ftp.mkd(path)
        except ftplib.error_perm as error:
            if not str(error).startswith("550"):
                raise
    for local, remote in [(KEY, f"/pocketjs/offload/{slot}.key"), (BINARY, "/3ds/larr-e.3dsx")]:
        payload = local.read_bytes()
        ftp.storbinary(f"STOR {remote}", io.BytesIO(payload), blocksize=65536)
        readback = io.BytesIO()
        ftp.retrbinary(f"RETR {remote}", readback.write)
        if readback.getvalue() != payload:
            sys.exit(f"Readback mismatch for {remote}; nothing is trustworthy, rerun deploy")
        print(f"verified {remote} ({len(payload)} bytes)")
    ftp.quit()
    print("Done. Close ftpd, then start LARR-E from the Homebrew Launcher.")


if __name__ == "__main__":
    main()
