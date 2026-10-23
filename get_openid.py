#!/usr/bin/env python3
"""监听 QQ 机器人网关事件，打印首个 C2C 私聊消息的 user_openid。

背景：QQ 开放平台控制台 UI 里不直接展示 user_openid，它只出现在机器人收到的
消息事件（C2C_MESSAGE_CREATE）的 author.user_openid 字段里。

用法（在你自己的服务器上，Python3）：
  pip install websocket-client
  QQ_BOT_APPID=<机器人AppID> QQ_BOT_SECRET=<机器人AppSecret> python3 get_openid.py

然后在 QQ 里给机器人发任意消息，脚本会打印出你的 user_openid 后自动退出。
把打印出的 openid 填进中转服务（qq_official_relay.py）的 QQ_USER_OPENID 即可。
按 Ctrl+C 可随时退出。

原理：
  1. AppID+AppSecret 换 Access Token（bots.qq.com，不受 IP 白名单限制）
  2. 取网关地址（GET /gateway）
  3. WebSocket 连网关，订阅 1<<25（群聊/单聊消息事件），等一条 C2C 消息
"""

import json
import os
import sys
import threading
import time
import urllib.request

try:
    import websocket
except ImportError:
    sys.exit("需要先安装依赖：pip install websocket-client")

APPID = os.environ.get("QQ_BOT_APPID", "").strip()
SECRET = os.environ.get("QQ_BOT_SECRET", "").strip()
if not APPID or not SECRET:
    sys.exit("请设置环境变量 QQ_BOT_APPID 和 QQ_BOT_SECRET")

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
INTENTS = 1 << 25  # 群聊/单聊消息事件（含 C2C_MESSAGE_CREATE）


def post_json(url, payload, headers=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


# 1. 换 Token
tok = post_json(TOKEN_URL, {"appId": APPID, "clientSecret": SECRET})
access_token = tok.get("access_token")
if not access_token:
    sys.exit(f"换 Token 失败（检查 AppID/AppSecret 是否正确）：{tok}")
print("Token 获取成功", flush=True)

# 2. 取网关地址
gw = get_json(f"{API_BASE}/gateway", headers={"Authorization": f"QQBot {access_token}"})
url = gw.get("url")
if not url:
    sys.exit(f"取网关地址失败：{gw}")

# 3. 连网关
ws = websocket.create_connection(url, timeout=30)
hello = json.loads(ws.recv())
interval = hello.get("d", {}).get("heartbeat_interval", 30000) / 1000.0

stop = False


def heartbeat():
    while not stop:
        time.sleep(interval)
        try:
            ws.send(json.dumps({"op": 1, "d": None}))
        except Exception:
            break


threading.Thread(target=heartbeat, daemon=True).start()
ws.send(
    json.dumps(
        {"op": 2, "d": {"token": f"QQBot {access_token}", "intents": INTENTS, "shard": [0, 1]}}
    )
)
print("已连接网关，现在去 QQ 里给机器人发任意消息…", flush=True)

try:
    while True:
        msg = json.loads(ws.recv())
        op, t, d = msg.get("op"), msg.get("t"), msg.get("d") or {}
        if op in (7, 9):  # 网关报错（如 intent 未授权）
            sys.exit(f"网关拒绝连接（op={op}）：{msg}")
        if op == 0 and t == "C2C_MESSAGE_CREATE":
            openid = (d.get("author") or {}).get("user_openid")
            print(f"\n抓到私聊消息！user_openid = {openid}")
            print(f"消息内容：{d.get('content')}")
            break
finally:
    stop = True
    try:
        ws.close()
    except Exception:
        pass

print("\n把上面的 user_openid 填进中转服务的 QQ_USER_OPENID 环境变量即可。")
