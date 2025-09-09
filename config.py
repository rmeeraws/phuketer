import os
from dotenv import load_dotenv

# Загружаем переменные из .env файла
load_dotenv()

# Ключи для API
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
GOOGLE_SEARCH_API_KEY = os.getenv("GOOGLE_SEARCH_API_KEY", "").strip()
CUSTOM_SEARCH_ENGINE_ID = os.getenv("CUSTOM_SEARCH_ENGINE_ID", "").strip()

# ↓↓↓ НОВОЕ для heartbeat ↓↓↓
HEALTHCHECKS_PING_URL = os.getenv("HEALTHCHECKS_PING_URL", "").strip()
# интервал пульса по умолчанию 300 сек (5 мин)
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "300"))