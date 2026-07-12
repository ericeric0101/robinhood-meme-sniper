from pathlib import Path
from pydantic import BaseModel
from dotenv import load_dotenv
import os

load_dotenv()


class Settings(BaseModel):
    app_env: str = os.getenv('APP_ENV', 'development')
    log_level: str = os.getenv('LOG_LEVEL', 'INFO')
    chain_id: str = os.getenv('CHAIN_ID', 'robinhood')
    data_dir: Path = Path(os.getenv('DATA_DIR', './data'))
    output_dir: Path = Path(os.getenv('OUTPUT_DIR', './outputs'))
    sqlite_path: Path = Path(os.getenv('SQLITE_PATH', './data/state.db'))
    tracking_db_path: Path = Path(os.getenv('TRACKING_DB_PATH', './data/tracking.db'))
    tracking_retention_days: int = int(os.getenv('TRACKING_RETENTION_DAYS', '180'))
    tracking_drop_stale_tokens_days: int | None = int(os.getenv('TRACKING_DROP_STALE_TOKENS_DAYS')) if os.getenv('TRACKING_DROP_STALE_TOKENS_DAYS') else None
    tracking_judge_enabled: bool = os.getenv('TRACKING_JUDGE_ENABLED', 'false').lower() in {'1', 'true', 'yes', 'on'}
    tracking_judge_endpoint: str | None = os.getenv('TRACKING_JUDGE_ENDPOINT')
    tracking_judge_api_key: str | None = os.getenv('TRACKING_JUDGE_API_KEY')
    tracking_judge_model: str | None = os.getenv('TRACKING_JUDGE_MODEL')
    tracking_judge_timeout_seconds: int = int(os.getenv('TRACKING_JUDGE_TIMEOUT_SECONDS', '30'))
    poll_interval_seconds: int = int(os.getenv('POLL_INTERVAL_SECONDS', '180'))
    x_provider: str = os.getenv('X_PROVIDER', 'apify')
    apify_api_token: str | None = os.getenv('APIFY_API_TOKEN')
    apify_x_actor_id: str = os.getenv('APIFY_X_ACTOR_ID', 'xtdata~twitter-x-scraper')
    twitterapiio_api_key: str | None = os.getenv('TWITTERAPIIO_API_KEY') or os.getenv('TWITTERAPI_IO_KEY')
    x_app_name: str | None = os.getenv('X_APP_NAME')
    x_client_id: str | None = os.getenv('X_CLIENT_ID')
    x_client_secret: str | None = os.getenv('X_CLIENT_SECRET')
    x_username: str | None = os.getenv('X_USERNAME')
    telegram_bot_token: str | None = os.getenv('TELEGRAM_BOT_TOKEN')
    telegram_chat_id: str | None = os.getenv('TELEGRAM_CHAT_ID')


settings = Settings()
