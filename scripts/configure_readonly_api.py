"""Enable/rotate/revoke integration access; never print the raw credential."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def configure(config_path, *, token_file=None, days=30, rotate=False, disable=False):
    config_path = Path(config_path)
    if config_path.is_symlink():
        raise ValueError("Refuse a symlink configuration file")
    if disable:
        config_path.unlink(missing_ok=True)
        return {"enabled": False}
    if not 1 <= days <= 90:
        raise ValueError("Token validity must be 1-90 days")
    if config_path.exists() and not rotate:
        raise ValueError("Configuration exists; use --rotate and a new token filename")
    if token_file is None:
        raise ValueError("--token-file is required; the token is never printed")
    token_file = Path(token_file)
    if token_file == config_path or token_file.is_symlink():
        raise ValueError("Token output must be a separate, new private file")
    token = secrets.token_urlsafe(36)
    now = datetime.now(timezone.utc)
    value = {
        "enabled": True,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=days)).isoformat(),
    }
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # O_EXCL and mode 0600 protect the one-time credential, including during creation.
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        fd, name = tempfile.mkstemp(prefix=".readonly-api-", dir=config_path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(value, indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, config_path)
        finally:
            temporary.unlink(missing_ok=True)
    except BaseException:
        token_file.unlink(missing_ok=True)
        raise
    return {
        "enabled": True,
        "expires_at": value["expires_at"],
        "token_file": str(token_file),
        "message": "Copy the token into secure cloud credential settings, then delete the token file. Do not paste it into chat.",
    }


def main():
    from config.settings import WEB_DATA_DIR

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-file", type=Path, default=Path(WEB_DATA_DIR) / "settings" / "readonly_api.json")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--rotate", action="store_true")
    parser.add_argument("--disable", action="store_true")
    args = parser.parse_args()
    try:
        result = configure(
            args.config_file, token_file=args.token_file, days=args.days, rotate=args.rotate, disable=args.disable
        )
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Configuration not changed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
