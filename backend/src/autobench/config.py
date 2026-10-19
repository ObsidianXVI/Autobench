"""Runtime settings, read from environment variables and the repo-level .env file."""

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_DIR / ".env", BACKEND_DIR / ".env"),
        extra="ignore",
    )

    # Field names map 1:1 to (case-insensitive) environment variable names, plus a few
    # commonly used aliases for the Alpaca credentials.
    alpaca_api_key_id: str = Field("", validation_alias=AliasChoices("ALPACA_API_KEY_ID", "ALPACA_API_KEY", "APCA_API_KEY_ID"))
    alpaca_api_secret_key: str = Field(
        "", validation_alias=AliasChoices("ALPACA_API_SECRET_KEY", "ALPACA_SECRET_KEY", "ALPACA_API_SECRET", "APCA_API_SECRET_KEY")
    )
    alpaca_data_feed: str = "sip"
    alpaca_trading_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_base_url: str = "https://data.alpaca.markets"

    autobench_bar_provider: str = "auto"  # auto | alpaca | yahoo | fake
    sec_user_agent: str = ""

    autobench_host: str = "127.0.0.1"
    autobench_port: int = 8000
    autobench_db_path: str = ""
    autobench_cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    autobench_background: bool = True  # scheduler: alerts, universe/macro refreshes (serve only)

    # Trading. The broker connects only under `autobench serve`. Paper accounts trade freely (within
    # the risk limits); a live account stays read-only unless this flag is set AND it is armed in the UI.
    autobench_broker: str = "ibkr"  # ibkr | sim (built-in simulator) | none
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4002  # IB Gateway paper (live 4001); TWS paper 7497 (live 7496)
    ibkr_client_id: int = 7
    ibkr_account: str = ""  # preferred account when the login holds several; the app can switch it
    autobench_allow_live_trading: bool = False

    @property
    def alpaca_enabled(self) -> bool:
        return bool(self.alpaca_api_key_id and self.alpaca_api_secret_key)

    @property
    def db_path(self) -> Path:
        return Path(self.autobench_db_path) if self.autobench_db_path else BACKEND_DIR / "data" / "autobench.db"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.autobench_cors_origins.split(",") if o.strip()]

    @property
    def bar_provider(self) -> str:
        choice = self.autobench_bar_provider.lower()
        if choice == "auto":
            return "alpaca" if self.alpaca_enabled else "yahoo"
        return choice


@lru_cache
def get_settings() -> Settings:
    return Settings()
