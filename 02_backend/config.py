from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BUNDLE_ENV_FILE = str(Path(__file__).resolve().parent.parent / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BUNDLE_ENV_FILE, ".env", "/etc/fcc/fcc.env"),
        env_prefix="FCC_",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = Field(...)
    database_schema: str = "fcc"
    session_secret: str = Field(...)
    host: str = "127.0.0.1"
    port: int = 8001
    log_level: str = "INFO"
    cors_origins: str = ""
    site_code: str = "PPA-BIB"
    timezone: str = "Asia/Makassar"
    evidence_dir: Path = Path("/var/lib/fcc/evidence")
    max_upload_mb: int = 8
    reconciliation_max_upload_mb: int = 50
    import_validation_cache_dir: Path = Path("/tmp/fcc-import-validation-cache")
    import_validation_cache_ttl_seconds: int = 1800
    cookie_secure: bool = True
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    session_max_age_seconds: int = 21600
    login_max_attempts: int = 5
    login_lock_minutes: int = 15

    discrepancy_daily_target_pct: float = 0.15
    discrepancy_weekly_target_pct: float = 0.15
    discrepancy_mtd_target_pct: float = 0.15
    discrepancy_stock_min_l: float = 2_500_000
    discrepancy_zero_outflow_tolerance_l: float = 1.0
    fuel_availability_target_days: float = 3.8
    discrepancy_opening_source: Literal["ACTUAL_PREVIOUS"] = "ACTUAL_PREVIOUS"
    allow_discrepancy_overrides: bool = False

    ss6_base_url: str = "https://ppa-bib.net"
    ss6_login_url: str = "https://ppa-bib.net/auth"
    ss6_export_url_template: str = (
        "https://ppa-bib.net/operation/export_ifcu/{date_from}/{date_to}/{shift}"
    )
    ss6_username: str = ""
    ss6_password: str = ""
    ss6_default_pwd: str = ""  # Optional legacy env alias; never populated in source.
    ss6_username_field: str = "nrp"
    ss6_password_field: str = "password"
    ss6_timeout_seconds: int = 90
    ss6_temp_ttl_seconds: int = 1800
    ss6_verify_tls: bool = True

    allow_public_register: bool = False
    upstream_url: str = "http://127.0.0.1:8001"
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 8765
    proxy_timeout_seconds: int = 300
    default_register_role: str = "FUELMAN"

    @field_validator("database_schema")
    @classmethod
    def validate_schema(cls, value: str) -> str:
        if not value.replace("_", "").isalnum():
            raise ValueError("database_schema contains invalid characters")
        return value

    @field_validator("session_secret")
    @classmethod
    def validate_secret(cls, value: str) -> str:
        if len(value) < 32:
            raise ValueError("FCC_SESSION_SECRET must be at least 32 characters")
        return value


    @property
    def cors_origins_list(self) -> list[str]:
        """Explicit cross-origin exceptions. Same-origin production needs none.

        FCC_CORS_ORIGINS accepts a comma-separated list such as
        https://fcc.example,https://field.example. Wildcards are intentionally
        not supported when credential cookies are used.
        """
        return [item.strip().rstrip("/") for item in self.cors_origins.split(",") if item.strip()]

    @property
    def evidence_dir_resolved(self) -> Path:
        return self.evidence_dir.expanduser().resolve()

    @property
    def import_validation_cache_dir_resolved(self) -> Path:
        return self.import_validation_cache_dir.expanduser().resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
