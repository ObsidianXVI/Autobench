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
    # Cloud Run behind Identity-Aware Proxy: the IAP audience (/projects/<number>/locations/<region>/services/<name>)
    # and the Google accounts allowed in. Set: every request must carry a valid IAP identity of an owner.
    autobench_iap_audience: str = ""
    # Cloud: the directory Claude Code sessions run in (the Autobench repo copy with CLAUDE.md and skills); its
    # .mcp.json is written at start so those sessions reach this server's MCP.
    autobench_workspace: str = ""
    autobench_owner_emails: str = ""
    autobench_background: bool = True  # scheduler: alerts, universe/macro refreshes (serve only)

    # Trading. The broker connects only under `autobench serve`. Paper accounts trade freely (within
    # the risk limits); a live account stays read-only unless this flag is set AND it is armed in the UI.
    autobench_broker: str = "ibkr"  # ibkr | sim (built-in simulator) | none
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4002  # IB Gateway paper (live 4001); TWS paper 7497 (live 7496)
    ibkr_client_id: int = 7
    ibkr_account: str = ""  # preferred account when the login holds several; the app can switch it
    autobench_allow_live_trading: bool = False
    # Cloud Run: "managed" runs IB Gateway inside the container on demand (services/broker/gateway.py);
    # empty: the operator runs IB Gateway/TWS. Mode is the IBC TradingMode (paper | live).
    autobench_gateway: str = ""
    autobench_gateway_mode: str = "paper"
    # IBC SecondFactorDevice: the 2FA method to pick when IBKR offers a list (spelled as in the list), "" = none
    autobench_gateway_2fa_device: str = ""
    autobench_gateway_idle_minutes: int = 30
    # IBKR Flex Web Service (statement downloads); may also be entered in the Portfolio page
    autobench_ibkr_flex_token: str = ""
    autobench_ibkr_flex_query: str = ""

    # Vral Altos cloud copy of the portfolio (services/cloud_sync.py); off when the project is empty
    autobench_cloud_sync_project: str = ""
    autobench_cloud_sync_interval: int = 300  # seconds between live pushes while IB Gateway is connected

    # Financier mode (services/financier.py): its Flutter web build, served at /financier/
    # (default: ../Financier/app/build/web next to this repo), and the Firebase project both modes
    # share (default: the cloud-sync project, else vral-altos). Read with the same Google login.
    autobench_financier_dir: str = ""

    # State store (state_store.py). Set: every table except the market-data caches lives in Firestore
    # (`<database>` of this project, collections autobench_*), and the SQLite file at db_path is only a
    # working copy loaded at start. Empty: local SQLite only (unit tests). Cloud Run sets it.
    autobench_state_project: str = ""
    autobench_state_database: str = "platform"
    # Cloud Storage bucket for the universe snapshots (data/universe/*.pkl), so a cold start does not
    # rebuild them. Empty: local files only.
    autobench_state_bucket: str = ""
    # single-writer lease (state_store.Mirror): how often an instance checks it still holds it; a starting instance
    # waits twice this before loading, so the one it replaces has stopped writing. 0 = no lease (tests, tools).
    autobench_state_lease_seconds: float = 3.0
    autobench_firebase_project: str = ""

    # Claude page: chat with Claude Code sessions on this machine (runs the `claude` command)
    autobench_claude_chat: bool = True
    autobench_claude_bin: str = ""  # default: `claude` on PATH or ~/.local/bin/claude
    autobench_claude_home: str = ""  # default: ~/.claude
    # Cloud: the Claude account to run as, an entry of the Vral Altos `claude-tokens` secret (one JSON object
    # tokenRef -> OAuth token, mounted as a file). Empty: the host's own Claude login.
    autobench_claude_token_ref: str = ""
    autobench_claude_tokens_file: str = "/secrets/claude-tokens/tokens.json"

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


# The `autobench-credentials` secret (Secret Manager, mounted as a JSON file on Cloud Run): its keys become
# environment variables unless already set. Written by tool/set_autobench_credentials.sh, never a repo file.
CREDENTIAL_KEYS = ("IBKR_USERNAME", "IBKR_PASSWORD", "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY", "SEC_USER_AGENT",
                   "AUTOBENCH_IBKR_FLEX_TOKEN", "AUTOBENCH_IBKR_FLEX_QUERY")


def load_credentials_file(path: str | None = None) -> list[str]:
    import json
    import os

    p = Path(path or os.environ.get("AUTOBENCH_CREDENTIALS_FILE", "/secrets/autobench/credentials.json"))
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return []
    loaded = []
    for k in CREDENTIAL_KEYS:
        v = data.get(k) if isinstance(data, dict) else None
        if isinstance(v, str) and v and not os.environ.get(k):
            os.environ[k] = v
            loaded.append(k)
    get_settings.cache_clear()
    return loaded
