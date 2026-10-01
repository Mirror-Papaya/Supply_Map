"""飞书长连接：接收消息事件 → bot.handle。本地电脑无需公网 IP。"""
import asyncio
import json
import logging
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from .. import bot, config, db, status
from . import client as fc

log = logging.getLogger("feishu.ws")
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bot")
_bot_open_id = ""


class FeishuResponder(bot.Responder):
    def __init__(self, message_id: str):
        self.mid = message_id

    def text(self, text):
        fc.reply(self.mid, "text", {"text": text})

    def card(self, card):
        fc.reply(self.mid, "interactive", card)

    def image(self, image_key):
        fc.reply(self.mid, "image", {"image_key": image_key})

    def file(self, data, name):
        key = fc.upload_file(data, name, "xls")
        fc.reply(self.mid, "file", {"file_key": key})

    def upload_image(self, png):
        return fc.upload_image(png)

    def download(self, key, kind):
        return fc.download_resource(self.mid, key, kind)


def _to_incoming(ev) -> bot.Incoming:
    msg = ev.message
    sender_id = ev.sender.sender_id.open_id if ev.sender and ev.sender.sender_id else ""
    mentions = msg.mentions or []
    if _bot_open_id:
        mentioned = any(m.id and m.id.open_id == _bot_open_id for m in mentions)
    else:
        mentioned = bool(mentions)
    try:
        content = json.loads(msg.content or "{}")
    except json.JSONDecodeError:
        content = {}
    created_at = ""
    try:      # 飞书的消息时间是毫秒时间戳：登记时间用"发出指令的时间"，而不是机器人处理的时间
        created_at = datetime.fromtimestamp(int(msg.create_time) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        pass
    return bot.Incoming(message_id=msg.message_id, chat_id=msg.chat_id, chat_type=msg.chat_type,
                        msg_type=msg.message_type, content=content, sender_id=sender_id,
                        mentioned_bot=mentioned, created_at=created_at)


def _process(ev):
    inc = _to_incoming(ev)
    try:
        inc.sender_name = fc.user_name(inc.sender_id)
        log.info("收到消息 %s %s %s from=%s", inc.chat_type, inc.msg_type, inc.message_id, inc.sender_name or inc.sender_id)
        bot.handle(inc, FeishuResponder(inc.message_id))
    except Exception as e:  # noqa: BLE001
        log.exception("处理消息失败")
        try:
            fc.reply(inc.message_id, "text", {"text": f"处理出错了：{e}"})
        except Exception:  # noqa: BLE001
            pass


def on_message(data: P2ImMessageReceiveV1):
    ev = data.event
    if not ev or not ev.message:
        return
    # 飞书没有及时收到确认会重推；按 message_id 去重，处理放到线程池里立即返回
    if not db.mark_message_processed(ev.message.message_id):
        return
    _pool.submit(_process, ev)


class _StatusHook(logging.Handler):
    """从 SDK 日志里判断连接状态，给首页展示。"""

    def emit(self, record):
        msg = record.getMessage()
        if "connected to" in msg and "disconnected" not in msg:
            status.set(feishu="已连接", feishu_error="")
        elif "disconnected" in msg or "reconnect" in msg:
            status.set(feishu="连接中")
        elif "connect failed" in msg:
            status.set(feishu="断开", feishu_error=msg[-200:])


def run_forever():
    global _bot_open_id
    # lark_oapi.ws 在模块级持有事件循环；在子线程里运行需要换成本线程的循环
    import lark_oapi.ws.client as wsc
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    wsc.loop = loop
    lark.logger.addHandler(_StatusHook())

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message).build())
    while True:
        try:
            if not _bot_open_id:
                try:
                    _bot_open_id = fc.bot_open_id()
                except Exception as e:  # noqa: BLE001
                    log.warning("获取机器人 open_id 失败（群聊中任何 @ 都会被当作 @机器人）：%s", e)
            status.set(feishu="连接中", feishu_error="")
            cli = lark.ws.Client(config.FEISHU_APP_ID, config.FEISHU_APP_SECRET, event_handler=handler,
                                 domain=config.FEISHU_BASE_URL, log_level=lark.LogLevel.INFO)
            cli.start()
        except Exception as e:  # noqa: BLE001
            log.exception("飞书长连接异常，10 秒后重连")
            status.set(feishu="断开", feishu_error=str(e)[:200])
            time.sleep(10)
