"""运行状态（给首页展示用）。"""
import threading

_lock = threading.Lock()
_state = {
    "feishu": "未配置",          # 未配置 / 连接中 / 已连接 / 断开
    "feishu_error": "",
    "bitable": "未配置",
    "bitable_last_sync": "",
    "bitable_error": "",
    "vision": "未配置",
    "started_at": "",
    "last_backup": "",
}


def set(**kw):
    with _lock:
        _state.update(kw)


def get() -> dict:
    with _lock:
        return dict(_state)
