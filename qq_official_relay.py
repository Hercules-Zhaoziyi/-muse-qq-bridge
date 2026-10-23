#!/usr/bin/env python3
"""QQ 通知推送中转服务（QQ 官方机器人 OpenAPI）.

架构：
  Muse(定时任务) --POST /push--> 本服务 --官方OpenAPI--> 你的 QQ 私聊

为什么需要这个中转：
  官方 API 需要 AppID + AppSecret 双密钥换 Token，而 Muse 的安全卡片只支持单密钥。
  所以双密钥放在你自己的服务器上，对外只暴露一个单密钥的 /push 接口给 Muse。

部署（在你自己的服务器上）：
  1. （IP 白名单：你的机器人当前未设置白名单，未设置时所有 IP 均可调用 API，
     如将来报错 11298 / 40023002 再去管理端「开发基础设置 → IP白名单」加服务器 IP）。
  2. 设置环境变量并运行（只需 Python3，无第三方依赖）：
       QQ_BOT_APPID=<机器人AppID> \
       QQ_BOT_SECRET=你的AppSecret \
       QQ_USER_OPENID=<你的user_openid> \
       PUSH_API_KEY=给Muse用的单密钥（自己编一个长随机串） \
       python3 qq_official_relay.py
     建议用 systemd / supervisor 托管，开机自启；生产环境建议套一层 HTTPS 反代。
  3. 把本服务的公网地址和 PUSH_API_KEY 发给 Muse（Muse 会存进安全卡片，只用于给你推消息）。

Muse 侧调用：
  POST http://你的服务器:8765/push
  Header: X-Api-Key: <PUSH_API_KEY>
  Body: {"content": "要推送的文本"}          # openid 可选，不传则用环境变量里的

接口说明（已实测可用）：
  换 Token: POST https://bots.qq.com/app/getAppAccessToken
            body {"appId","clientSecret"}，有效期 7200 秒
  发私聊:   POST https://api.sgroup.qq.com/v2/users/{openid}/messages
            Header: Authorization: QQBot {token}
            body {"msg_type":0,"content":"文本"}
"""

import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

APPID = os.environ["QQ_BOT_APPID"]
SECRET = os.environ["QQ_BOT_SECRET"]
DEFAULT_OPENID = os.environ.get("QQ_USER_OPENID", "")
API_KEY = os.environ["PUSH_API_KEY"]
PORT = int(os.environ.get("PORT", "8765"))

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"

_token = None
_token_exp = 0.0
_token_lock = threading.Lock()


def _post_json(url, payload, headers=None):
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def get_token():
    global _token, _token_exp
    with _token_lock:
        if _token and time.time() < _token_exp - 300:
            return _token
        body = _post_json(TOKEN_URL, {"appId": APPID, "clientSecret": SECRET})
        if "access_token" not in body:
            raise RuntimeError(f"getAppAccessToken failed: {body}")
        _token = body["access_token"]
        _token_exp = time.time() + int(body.get("expires_in", 7200))
        return _token


def send_dm(openid, content):
    token = get_token()
    return _post_json(
        f"{API_BASE}/v2/users/{openid}/messages",
        {"msg_type": 0, "content": content},
        headers={"Authorization": f"QQBot {token}"},
    )


class Handler(BaseHTTPRequestHandler):
    def _resp(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._resp(200, {"ok": True})
        return self._resp(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/push":
            return self._resp(404, {"ok": False, "error": "not found"})
        if self.headers.get("X-Api-Key") != API_KEY:
            return self._resp(401, {"ok": False, "error": "bad api key"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._resp(400, {"ok": False, "error": "bad json"})
        content = (payload.get("content") or "").strip()
        openid = payload.get("openid") or DEFAULT_OPENID
        if not content:
            return self._resp(400, {"ok": False, "error": "content required"})
        if not openid:
            return self._resp(400, {"ok": False, "error": "openid required"})
        try:
            qq_resp = send_dm(openid, content)
        except Exception as e:  # noqa: BLE001 - report transport errors plainly
            return self._resp(502, {"ok": False, "error": f"qq api failed: {e}"})
        self._resp(200, {"ok": True, "qq": qq_resp})

    def log_message(self, *args):  # keep logs quiet; never log message content
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
