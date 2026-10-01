"""配置检查与初始化工具。

  python run_setup.py check                 检查飞书应用 / Claude API 配置
  python run_setup.py bitable <多维表格链接>  在该多维表格里创建"库存登记"数据表和表单，并写入 .env
  python run_setup.py chats                 列出机器人所在的群（用来设置拣货群 PICK_CHAT_IDS）
"""
import json
import re
import sys

from . import config, db


def set_env(key: str, value: str):
    path = config.ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for i, ln in enumerate(lines):
        if re.match(rf"^\s*{re.escape(key)}\s*=", ln):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def check():
    ok = True
    print("== 飞书应用 ==")
    if not config.feishu_enabled():
        print("  ✗ .env 里没有 FEISHU_APP_ID / FEISHU_APP_SECRET")
        ok = False
    else:
        from .feishu import client as fc
        try:
            print(f"  ✓ 应用凭证有效，机器人 open_id = {fc.bot_open_id()}")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {e}")
            ok = False
    print("== 图片识别 ==")
    provider = config.vision_provider()
    if provider == "deepseek":
        import httpx
        try:
            r = httpx.get(f"{config.DEEPSEEK_BASE_URL}/models", headers={"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"},
                          timeout=20)
            if r.status_code == 401:
                raise RuntimeError("DEEPSEEK_API_KEY 无效")
            r.raise_for_status()
            ids = [m.get("id") for m in r.json().get("data", [])]
            if config.DEEPSEEK_MODEL in ids:
                print(f"  ✓ DeepSeek API Key 有效，模型 {config.DEEPSEEK_MODEL} 可用")
            else:
                print(f"  ✗ API Key 有效，但没有模型 {config.DEEPSEEK_MODEL}（可用：{', '.join(ids)}）")
                ok = False
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {e}")
            ok = False
    elif not config.ANTHROPIC_API_KEY:
        print("  ✗ .env 里没有 DEEPSEEK_API_KEY（或 ANTHROPIC_API_KEY）")
        ok = False
    else:
        import anthropic
        try:
            m = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY).models.retrieve(config.CLAUDE_MODEL)
            print(f"  ✓ API Key 有效，模型 {m.id} 可用")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {e}")
            ok = False
    print("== 多维表格 ==")
    if config.bitable_enabled():
        from .feishu import bitable_sync
        try:
            n = len(bitable_sync._pending_records())
            print(f"  ✓ 可以读取数据表（待处理 {n} 条）")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {e}")
            ok = False
    else:
        print("  - 尚未配置（运行：python run_setup.py bitable <多维表格链接>）")
    return ok


def bitable(url_or_token: str):
    m = re.search(r"/base/([A-Za-z0-9]+)", url_or_token)
    token = m.group(1) if m else url_or_token.strip()
    if "/wiki/" in url_or_token:
        print("这是知识库里的多维表格链接，请在多维表格右上角「…→ 复制链接」拿到 /base/ 开头的链接，"
              "或者在云文档里新建一个独立的多维表格。")
        return False
    from .feishu import bitable_sync
    db.connect()
    table_id = bitable_sync.create_table(token)
    set_env("BITABLE_APP_TOKEN", token)
    set_env("BITABLE_TABLE_ID", table_id)
    print(f"✓ 已创建数据表 table_id={table_id}，并写入 .env。重启程序后开始同步。")
    return True


def chats():
    import lark_oapi as lark
    from .feishu import client as fc
    req = (lark.BaseRequest.builder().http_method(lark.HttpMethod.GET).uri("/open-apis/im/v1/chats?page_size=100")
           .token_types({lark.AccessTokenType.TENANT}).build())
    data = json.loads(fc.client().request(req).raw.content)
    if data.get("code") != 0:
        print(data)
        return False
    for c in data["data"].get("items", []):
        print(f"{c['chat_id']}  {c.get('name', '')}")
    return True


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "check"
    if cmd == "check":
        return check()
    if cmd == "bitable" and len(argv) > 2:
        return bitable(argv[2])
    if cmd == "chats":
        return chats()
    print(__doc__)
    return False


if __name__ == "__main__":
    sys.exit(0 if main(sys.argv) else 1)
