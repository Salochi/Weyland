"""Encryption for Plaid access tokens at rest.

The key lives in a separate root:weyland 0640 file, so a copied database (or a
backup of /var/lib/weyland) does not carry usable tokens. It does not protect
against root on the box; nothing on the box can.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet


def create_key(path: Path) -> bool:
    """Create the key file if missing. Returns True if a new key was written."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(fd, "wb") as fh:
        fh.write(Fernet.generate_key())
    return True


class TokenBox:
    def __init__(self, key_path: Path):
        self._fernet = Fernet(key_path.read_bytes().strip())

    def seal(self, token: str) -> bytes:
        return self._fernet.encrypt(token.encode())

    def open(self, blob: bytes) -> str:
        return self._fernet.decrypt(blob).decode()
