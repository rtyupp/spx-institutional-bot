"""إعدادات بوت SPX المؤسسي — كل الأسرار من متغيرات البيئة (.env محلياً / GitHub Secrets)."""
import os
from pathlib import Path


def _load_dotenv() -> None:
    p = Path(__file__).with_name(".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0") or 0)
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip() or str(ADMIN_CHAT_ID)

# Capital.com Demo API
CAPITAL_EMAIL = os.environ.get("CAPITAL_EMAIL", "")
CAPITAL_API_KEY = os.environ.get("CAPITAL_API_KEY", "")
CAPITAL_API_PASSWORD = os.environ.get("CAPITAL_API_PASSWORD", "")
CAPITAL_AUTO_TRADE = os.environ.get("CAPITAL_AUTO_TRADE", "0") == "1"
TRADE_SIZE = float(os.environ.get("TRADE_SIZE", "0.1"))

# إعدادات التنبيهات
SIGNAL_INTERVAL_SEC = int(os.environ.get("SIGNAL_INTERVAL_SEC", "300"))
SIGNAL_COOLDOWN_MIN = int(os.environ.get("SIGNAL_COOLDOWN_MIN", "180"))
MIN_SIGNAL_SCORE = float(os.environ.get("MIN_SIGNAL_SCORE", "2.0"))
STATE_FILE = os.environ.get("STATE_FILE", str(Path(__file__).with_name("state.json")))
