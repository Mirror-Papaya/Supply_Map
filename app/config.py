"""运行配置：从项目根目录的 .env 读取。"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR") or ROOT / "data")
DB_PATH = DATA_DIR / "warehouse.db"
MAP_ASSET_DIR = DATA_DIR / "maps"
BACKUP_DIR = DATA_DIR / "backups"
LOG_DIR = DATA_DIR / "logs"
WEB_DIR = ROOT / "web"

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "").strip()
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "").strip()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5").strip()
CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT", "medium").strip()   # low / medium / high：识别细致度与速度的平衡

BITABLE_APP_TOKEN = os.getenv("BITABLE_APP_TOKEN", "").strip()
BITABLE_TABLE_ID = os.getenv("BITABLE_TABLE_ID", "").strip()
BITABLE_POLL_SECONDS = int(os.getenv("BITABLE_POLL_SECONDS", "20"))

# 拣货群 chat_id，逗号分隔；为空时机器人所在的所有群都响应图片
PICK_CHAT_IDS = {c.strip() for c in os.getenv("PICK_CHAT_IDS", "").split(",") if c.strip()}

WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1").strip()
WEB_PORT = int(os.getenv("WEB_PORT", "8765"))

# 1 = SKU 必须在字典里才允许登记；0 = 允许登记字典外的 SKU（给出提示）
STRICT_SKU = os.getenv("STRICT_SKU", "0").strip() == "1"

FONT_PATH = os.getenv("FONT_PATH", r"C:\Windows\Fonts\msyh.ttc")

for d in (DATA_DIR, MAP_ASSET_DIR, BACKUP_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)


def feishu_enabled() -> bool:
    return bool(FEISHU_APP_ID and FEISHU_APP_SECRET)


def bitable_enabled() -> bool:
    return feishu_enabled() and bool(BITABLE_APP_TOKEN and BITABLE_TABLE_ID)
