import os


def _parse_admin_ids(raw_value):
    if not raw_value:
        return []
    return [int(item.strip()) for item in raw_value.split(",") if item.strip()]


TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
SHOP_ID = int(os.getenv("SHOP_ID", "0") or "0")
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN", "")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "DeloDushiBot")
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "credentials.json")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")


try:
    from config_local import *  # noqa: F401,F403
except ImportError:
    pass
