#!/usr/bin/env python3
"""QQ 事件监听 → 邮件转发：把你发给机器人的私聊消息转发给 Muse（反向链路）。

架构（反向）：
  你在 QQ 给机器人发消息 --网关事件--> 本脚本 --SMTP(QQ邮箱)--> ziyi的Gmail
       --Muse 定时轮询 Gmail--> Muse 处理 --回复走正向链路--> QQ 推回给你

注意：正向推送走主动消息接口，不会产生 C2C 事件，所以不会死循环。

部署（和中转服务同一台服务器，Python3）：
  pip install websocket-client
  QQ_BOT_APPID=<机器人AppID> \
  QQ_BOT_SECRET=<机器人AppSecret> \
  QQ_USER_OPENID=<你的openid，只转发这个人的消息> \
  QQ_MAIL_USER=<你的QQ邮箱> \
  QQ_MAIL_AUTHCODE=<QQ邮箱授权码> \
  FORWARD_TO=<助手的Gmail地址> \
  python3 qq_event_listener.py
用 systemd 托管（照 qq_push_relay.service 改 ExecStart 和 Environment 即可）。
get_openid.py 拿到 openid 后就不需要了，不要和本脚本同时跑。
"""

import json
import os
import smtplib
import sys
import threading
import time
import urllib.request
from email.mime.text import MIMEText

try:
    import websocket
except ImportError:
    sys.exit("需要先安装依赖：pip install websocket-client")

APPID = os.environ.get("QQ_BOT_APPID", "").strip()
SECRET = os.environ.get("QQ_BOT_SECRET", "").strip()
MY_OPENID = os.environ.get("QQ_USER_OPENID", "").strip()
MAIL_USER = os.environ.get("QQ_MAIL_USER", "").strip()
MAIL_AUTHCODE = os.environ.get("QQ_MAIL_AUTHCODE", "").strip()
FORWARD_TO = os.environ.get("FORWARD_TO", "").strip()
CMD_PREFIX = "[MuseCmd]"

for name, val in [("QQ_BOT_APPID", APPID), ("QQ_BOT_SECRET", SECRET),
                  ("QQ_USER_OPENID", MY_OPENID), ("QQ_MAIL_USER", MAIL_USER),
                  ("QQ_MAIL_AUTHCODE", MAIL_AUTHCODE), ("FORWARD_TO", FORWARD_TO)]:
    if not val:
        sys.exit(f"请设置环境变量 {name}")

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
INTENTS = 1 << 25  # 群聊/单聊消息事件


def post_json(url, payload, headers=None):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def forward_to_muse(content):
    msg = MIMEText(content, "plain", "utf-8")
    msg["Subject"] = CMD_PREFIX
    msg["From"] = MAIL_USER
    msg["To"] = FORWARD_TO
    with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=20) as s:
        s.login(MAIL_USER, MAIL_AUTHCODE)
        s.send_message(msg)


def listen_forever():
    backoff = 5
    while True:
        try:
            tok = post_json(TOKEN_URL, {"appId": APPID, "clientSecret": SECRET})
            access_token = tok.get("access_token")
            if not access_token:
                print(f"换 Token 失败：{tok}，{backoff}s 后重试", flush=True)
                time.sleep(backoff)
                continue
            gw = get_json(f"{API_BASE}/gateway",
                          headers={"Authorization": f"QQBot {access_token}"})
            url = gw.get("url")
            if not url:
                print(f"取网关失败：{gw}，{backoff}s 后重试", flush=True)
                time.sleep(backoff)
                continue

            ws = websocket.create_connection(url, timeout=40)
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
            ws.send(json.dumps({"op": 2, "d": {
                "token": f"QQBot {access_token}", "intents": INTENTS, "shard": [0, 1]}}))
            print("网关已连接，等待你的 QQ 消息…", flush=True)
            backoff = 5

            while True:
                raw = ws.recv()
                msg = json.loads(raw)
                op, t, d = msg.get("op"), msg.get("t"), msg.get("d") or {}
                if op in (7, 9):
                    print(f"网关拒绝（op={op}）：{msg}", flush=True)
                    break
                if op == 0 and t == "C2C_MESSAGE_CREATE":
                    author = d.get("author") or {}
                    if author.get("user_openid") != MY_OPENID:
                        continue
                    content = (d.get("content") or "").strip()
                    if not content:
                        continue
                    print(f"收到你的消息，转交 Muse：{content[:60]}", flush=True)
                    try:
                        forward_to_muse(content)
                        print("已转发到邮箱", flush=True)
                    except Exception as e:
                        print(f"邮件转发失败：{e}", flush=True)
        except Exception as e:
            print(f"连接断开（{e}），{backoff}s 后重连…", flush=True)
        finally:
            try:
                stop = True
                ws.close()
            except Exception:
                pass
        time.sleep(backoff)
        backoff = min(backoff * 2, 120)


if __name__ == "__main__":
    listen_forever()
