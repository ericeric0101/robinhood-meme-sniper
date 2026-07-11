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
    poll_interval_seconds: int = int(os.getenv('POLL_INTERVAL_SECONDS', '180'))
    apify_api_token: str | None = os.getenv('APIFY_API_TOKEN')
    apify_x_actor_id: str = os.getenv('APIFY_X_ACTOR_ID', 'xtdata~twitter-x-scraper')
    x_app_name: str | None = os.getenv('X_APP_NAME')
    x_client_id: str | None = os.getenv('X_CLIENT_ID')
    x_client_secret: str | None = os.getenv('X_CLIENT_SECRET')
    x_username: str | None = os.getenv('X_USERNAME')
    telegram_bot_token: str | None = os.getenv('TELEGRAM_BOT_TOKEN')
    telegram_chat_id: str | None = os.getenv('TELEGRAM_CHAT_ID')


settings = Settings()
