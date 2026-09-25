"""Print the three secrets this application refuses to default.

    python -m scripts.keys

Each one has no default on purpose, and each failure is loud at the point of
use rather than silent: a shared default signing key is a signature anyone can
forge, and a shared default encryption key is plaintext with extra steps.
"""
from __future__ import annotations

import secrets

from app.engine.secrets import new_key


def main() -> int:
    print("# Paste these into .env. Keep them out of version control.")
    print("# Changing FERNET_KEY makes every stored refresh token unreadable,")
    print("# and every user has to reconnect Gmail. Changing the others signs")
    print("# everyone out, which is harmless.")
    print()
    print(f"JWT_SECRET={secrets.token_urlsafe(48)}")
    print(f"CONFIRM_TOKEN_SECRET={secrets.token_urlsafe(48)}")
    print(f"FERNET_KEY={new_key()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
