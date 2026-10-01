"""程序入口：本地网页（地图编辑器/管理页）+ 飞书机器人长连接 + 多维表格同步 + 每日备份。

运行：.venv\\Scripts\\python -m app.main
"""
import logging
import logging.handlers
import threading
import time
from datetime import datetime

import uvicorn

from . import config, db, status


def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(config.LOG_DIR / "app.log", maxBytes=5_000_000, backupCount=5,
                                              encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = [fh, sh]


def backup_loop(keep_days=14):
    log = logging.getLogger("backup")
    while True:
        try:
            name = config.BACKUP_DIR / f"warehouse-{datetime.now():%Y%m%d}.db"
            if not name.exists():
                db.backup(name)
                log.info("已备份数据库到 %s", name)
            backups = sorted(config.BACKUP_DIR.glob("warehouse-*.db"))
            for old in backups[:-keep_days]:
                old.unlink()
            status.set(last_backup=datetime.now().strftime("%Y-%m-%d %H:%M"))
        except Exception:  # noqa: BLE001
            log.exception("备份失败")
        time.sleep(3600)


def main():
    setup_logging()
    log = logging.getLogger("main")
    db.connect()
    status.set(started_at=db.now(), vision="已配置" if config.ANTHROPIC_API_KEY else "未配置")
    threading.Thread(target=backup_loop, name="backup", daemon=True).start()

    if config.feishu_enabled():
        from .feishu import gateway
        threading.Thread(target=gateway.run_forever, name="feishu-ws", daemon=True).start()
        if config.bitable_enabled():
            from .feishu import bitable_sync
            threading.Thread(target=bitable_sync.run_forever, name="bitable-sync", daemon=True).start()
    else:
        log.warning("未配置飞书 App ID / Secret，只启动本地网页")

    log.info("本地网页：http://%s:%s", config.WEB_HOST, config.WEB_PORT)
    uvicorn.run("app.web:app", host=config.WEB_HOST, port=config.WEB_PORT, log_level="warning")


if __name__ == "__main__":
    main()
