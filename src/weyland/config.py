"""Settings loader.

Values come from ``$WEYLAND_CONFIG_DIR/plaid.env`` (secrets, edited by the user) and
``$WEYLAND_CONFIG_DIR/weyland.env`` (non-secret knobs), then process env overrides.
Secrets are never logged or included in ``repr``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PLAID_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}


class ConfigError(RuntimeError):
    pass


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


@dataclass(frozen=True)
class Settings:
    plaid_env: str
    client_id: str = field(repr=False)
    secret: str = field(repr=False)
    db_path: Path
    key_path: Path
    allowed_users: frozenset[str]
    redirect_uri: str | None = None
    days_requested: int = 730

    @property
    def base_url(self) -> str:
        return PLAID_HOSTS[self.plaid_env]

    @property
    def is_production(self) -> bool:
        return self.plaid_env == "production"


def load_settings(plaid_env: str | None = None) -> Settings:
    config_dir = Path(os.environ.get("WEYLAND_CONFIG_DIR", "/etc/weyland"))
    values: dict[str, str] = {}
    values.update(read_env_file(config_dir / "weyland.env"))
    values.update(read_env_file(config_dir / "plaid.env"))
    values.update({k: v for k, v in os.environ.items() if k.startswith(("PLAID_", "WEYLAND_"))})

    env = (plaid_env or values.get("WEYLAND_PLAID_ENV") or "sandbox").lower()
    if env not in PLAID_HOSTS:
        raise ConfigError(f"WEYLAND_PLAID_ENV must be one of {sorted(PLAID_HOSTS)}, got {env!r}")

    client_id = values.get("PLAID_CLIENT_ID", "")
    secret = values.get(f"PLAID_SECRET_{env.upper()}", "")
    if not client_id or not secret:
        raise ConfigError(
            f"PLAID_CLIENT_ID and PLAID_SECRET_{env.upper()} must be set in {config_dir / 'plaid.env'}"
        )

    allowed = frozenset(
        u.strip().lower() for u in values.get("WEYLAND_ALLOWED_USERS", "").split(",") if u.strip()
    )
    data_dir = Path(values.get("WEYLAND_DATA_DIR", "/var/lib/weyland"))
    return Settings(
        plaid_env=env,
        client_id=client_id,
        secret=secret,
        # One DB per Plaid environment so sandbox fixtures never mix with real data.
        db_path=data_dir / f"weyland-{env}.db",
        key_path=Path(values.get("WEYLAND_KEY_PATH", str(config_dir / "token.key"))),
        allowed_users=allowed,
        redirect_uri=values.get("WEYLAND_REDIRECT_URI") or None,
        days_requested=int(values.get("WEYLAND_DAYS_REQUESTED", "730")),
    )
