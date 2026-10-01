"""飞书 Open API 封装：发消息、上传图片/文件、下载消息资源、查用户姓名。"""
import io
import json
import logging
import threading

import lark_oapi as lark
from lark_oapi.api.contact.v3 import GetUserRequest
from lark_oapi.api.im.v1 import (CreateFileRequest, CreateFileRequestBody, CreateImageRequest,
                                 CreateImageRequestBody, CreateMessageRequest, CreateMessageRequestBody,
                                 GetMessageResourceRequest, ReplyMessageRequest, ReplyMessageRequestBody)

from .. import config

log = logging.getLogger("feishu")
_client = None
_lock = threading.Lock()
_names: dict = {}


class FeishuError(Exception):
    pass


def client() -> lark.Client:
    global _client
    with _lock:
        if _client is None:
            _client = (lark.Client.builder().app_id(config.FEISHU_APP_ID).app_secret(config.FEISHU_APP_SECRET)
                       .domain(config.FEISHU_BASE_URL).log_level(lark.LogLevel.WARNING).build())
        return _client


def _check(resp, what):
    if not resp.success():
        raise FeishuError(f"{what}失败：code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}")
    return resp


def reply(message_id: str, msg_type: str, content: dict) -> str:
    req = (ReplyMessageRequest.builder().message_id(message_id)
           .request_body(ReplyMessageRequestBody.builder().msg_type(msg_type)
                         .content(json.dumps(content, ensure_ascii=False)).build()).build())
    resp = _check(client().im.v1.message.reply(req), "回复消息")
    return resp.data.message_id


def send(receive_id: str, msg_type: str, content: dict, id_type: str = "chat_id") -> str:
    req = (CreateMessageRequest.builder().receive_id_type(id_type)
           .request_body(CreateMessageRequestBody.builder().receive_id(receive_id).msg_type(msg_type)
                         .content(json.dumps(content, ensure_ascii=False)).build()).build())
    resp = _check(client().im.v1.message.create(req), "发送消息")
    return resp.data.message_id


def upload_image(png: bytes) -> str:
    req = (CreateImageRequest.builder()
           .request_body(CreateImageRequestBody.builder().image_type("message").image(io.BytesIO(png)).build())
           .build())
    return _check(client().im.v1.image.create(req), "上传图片").data.image_key


def upload_file(data: bytes, file_name: str, file_type: str = "xls") -> str:
    req = (CreateFileRequest.builder()
           .request_body(CreateFileRequestBody.builder().file_type(file_type).file_name(file_name)
                         .file(io.BytesIO(data)).build()).build())
    return _check(client().im.v1.file.create(req), "上传文件").data.file_key


def download_resource(message_id: str, file_key: str, kind: str = "image") -> bytes:
    req = GetMessageResourceRequest.builder().message_id(message_id).file_key(file_key).type(kind).build()
    resp = _check(client().im.v1.message_resource.get(req), "下载消息资源")
    return resp.file.read()


def user_name(open_id: str) -> str:
    """open_id → 姓名（缓存）。没有通讯录权限时返回空字符串。"""
    if not open_id:
        return ""
    if open_id in _names:
        return _names[open_id]
    name = ""
    try:
        req = GetUserRequest.builder().user_id(open_id).user_id_type("open_id").build()
        resp = client().contact.v3.user.get(req)
        if resp.success() and resp.data and resp.data.user:
            name = resp.data.user.name or ""
        else:
            log.warning("查询用户姓名失败 code=%s msg=%s（检查通讯录权限与权限范围）", resp.code, resp.msg)
    except Exception:  # noqa: BLE001
        log.exception("查询用户姓名失败")
    if name:      # 查不到（权限还没开通等）不缓存，下次还会重试
        _names[open_id] = name
    return name


def bot_open_id() -> str:
    req = (lark.BaseRequest.builder().http_method(lark.HttpMethod.GET).uri("/open-apis/bot/v3/info")
           .token_types({lark.AccessTokenType.TENANT}).build())
    resp = client().request(req)
    data = json.loads(resp.raw.content)
    if data.get("code") != 0:
        raise FeishuError(f"获取机器人信息失败：{data}")
    return data["bot"]["open_id"]
