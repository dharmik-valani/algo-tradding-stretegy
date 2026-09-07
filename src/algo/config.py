from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "settings.yaml"


def load_yaml() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open() as fh:
        return yaml.safe_load(fh) or {}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    dhan_client_id: str = ""
    dhan_access_token: str = ""
    dhan_sandbox_client_id: str = ""
    dhan_sandbox_access_token: str = ""
    # Optional: auto-mint a new token after full expiry (TOTP must be enabled on Dhan).
    dhan_pin: str = ""
    dhan_totp_secret: str = ""
    dhan_mode: str = "live"  # live | sandbox (sandbox = API dry-run host only)
    database_url: str = "sqlite:///./data/algo.db"
    log_level: str = "INFO"
    paper_ui_host: str = "127.0.0.1"
    paper_ui_port: int = 8787
    # public = Yahoo. dhan = Data API LTP/OHLC. auto = Dhan then Yahoo fallback.
    paper_price_source: str = "auto"
    yaml_config: dict = Field(default_factory=load_yaml)

    @property
    def data_dir(self) -> Path:
        return (ROOT / "data").resolve()

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def parquet_dir(self) -> Path:
        return self.data_dir / "parquet"

    @property
    def calendar_path(self) -> Path:
        rel = self.yaml_config.get("market", {}).get("calendar_file", "data/calendars/nse.yaml")
        return ROOT / rel

    @property
    def dhan_base_url(self) -> str:
        if self.dhan_mode.strip().lower() == "sandbox":
            return "https://sandbox.dhan.co/v2"
        return self.yaml_config.get("dhan", {}).get("base_url", "https://api.dhan.co/v2")

    def active_dhan_credentials(self) -> tuple[str, str]:
        """Credentials for the configured DHAN_MODE host.

        Live paper LTP and historical download should use live credentials.
        Sandbox credentials are only for order-API shape tests.
        """
        if self.dhan_mode.strip().lower() == "sandbox":
            return self.dhan_sandbox_client_id, self.dhan_sandbox_access_token
        return self.dhan_client_id, self.dhan_access_token

    @property
    def instrument_master_url(self) -> str:
        return self.yaml_config.get("dhan", {}).get(
            "instrument_master_url",
            "https://images.dhan.co/api-data/api-scrip-master.csv",
        )

    @property
    def requests_per_second(self) -> float:
        return float(
            self.yaml_config.get("dhan", {}).get("rate_limit", {}).get("requests_per_second", 5)
        )

    @property
    def requests_per_day(self) -> int:
        return int(
            self.yaml_config.get("dhan", {}).get("rate_limit", {}).get("requests_per_day", 100000)
        )

    @property
    def timeout_seconds(self) -> float:
        return float(self.yaml_config.get("dhan", {}).get("timeout_seconds", 30))

    @property
    def intraday_chunk_days(self) -> int:
        return int(self.yaml_config.get("historical_data", {}).get("intraday_chunk_days", 90))

    @property
    def daily_chunk_days(self) -> int:
        return int(self.yaml_config.get("historical_data", {}).get("daily_chunk_days", 365))

    @property
    def save_raw(self) -> bool:
        return bool(self.yaml_config.get("historical_data", {}).get("save_raw", True))

    @property
    def max_retries(self) -> int:
        return int(self.yaml_config.get("historical_data", {}).get("max_retries", 5))

    @property
    def universe_indices(self) -> list[str]:
        return list(self.yaml_config.get("universe", {}).get("indices", ["NIFTY"]))

    @property
    def storage_tz(self) -> str:
        return self.yaml_config.get("timezone", {}).get("storage", "UTC")

    @property
    def market_tz(self) -> str:
        return self.yaml_config.get("timezone", {}).get("market", "Asia/Kolkata")

    def resolve_db_url(self) -> str:
        url = (self.database_url or "").strip()
        if url.startswith("sqlite:///./"):
            rel = url.removeprefix("sqlite:///./")
            return f"sqlite:///{(ROOT / rel).resolve()}"
        # Supabase / Heroku often give postgres:// — SQLAlchemy 2 wants postgresql+psycopg://
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url.removeprefix("postgres://")
        elif url.startswith("postgresql://") and "+psycopg" not in url:
            url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
        return url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
