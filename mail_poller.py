#!/usr/bin/env python3
"""QQ邮箱轮询桥：轮询 QQ邮箱 IMAP，把 Muse 发来的推送邮件转发到本地 QQ 中转服务。

架构：
  Muse --Gmail--> 你的QQ邮箱 --IMAP轮询--> 本脚本
       --POST http://127.0.0.1:8765/push--> qq_official_relay.py --QQ API--> 你的QQ

为什么用邮件桥：
  Muse 的沙箱网络直连不到你的服务器（透明劫持），ntfy.sh 等国外服务双方又都连不上。
  Gmail（Muse 侧已接入）和 QQ邮箱 IMAP（国内）双方都通，不需要任何入站端口。

部署（和中转服务同一台服务器，Python3 无第三方依赖）：
  1. QQ邮箱 → 设置 → 账户 → 开启 "IMAP/SMTP 服务"，按提示用密保手机生成「授权码」
     （注意：这里用授权码，不是 QQ 密码）。
  2. QQ_MAIL_USER=<你的QQ邮箱> \
     QQ_MAIL_AUTHCODE=<QQ邮箱授权码> \
     PUSH_API_KEY=<中转服务的单密钥> \
     EXPECTED_FROM=<助手的Gmail地址> \
     python3 mail_poller.py
  3. 用 systemd 托管（照 qq_push_relay.service 改 ExecStart 和 Environment 即可）。

Muse 侧发件约定：
  收件人：QQ_MAIL_USER
  主题：[MusePush] 开头
  正文：纯文本推送内容（脚本只取 text/plain 部分）
"""

import datetime
import email
import email.header
import imaplib
import json
import os
import sys
import time
import urllib.request

MAIL_USER = os.environ.get("QQ_MAIL_USER", "").strip()
MAIL_AUTHCODE = os.environ.get("QQ_MAIL_AUTHCODE", "").strip()
API_KEY = os.environ.get("PUSH_API_KEY", "").strip()
EXPECTED_FROM = os.environ.get("EXPECTED_FROM", "").strip().lower()
RELAY = os.environ.get("RELAY_URL", "http://127.0.0.1:8765/push").rstrip("/")
SUBJECT_PREFIX = "[MusePush]"
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))

if not MAIL_USER or not MAIL_AUTHCODE or not API_KEY:
    sys.exit("请设置环境变量 QQ_MAIL_USER、QQ_MAIL_AUTHCODE、PUSH_API_KEY、EXPECTED_FROM")

# 已处理邮件的 UID 记录（防重发）。不用 IMAP 的已读/未读标记做去重，
# 因为你在邮箱 App 里点开看一眼就会变已读，导致推送丢失。
SEEN_FILE = os.environ.get(
    "SEEN_UIDS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".poller_seen_uids.json"),
)


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen):
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(seen)[-500:], f)
    except Exception as e:
        print(f"保存已处理记录失败：{e}", flush=True)


seen_uids = load_seen()


def decode_header_value(value):
    parts = email.header.decode_header(value or "")
    out = ""
    for text, charset in parts:
        if isinstance(text, bytes):
            out += text.decode(charset or "utf-8", "replace")
        else:
            out += text
    return out


def get_text_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, "replace")
        return ""
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, "replace")


def push_to_qq(content):
    data = json.dumps({"content": content}, ensure_ascii=False).encode()
    req = urllib.request.Request(
        RELAY,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8", "X-Api-Key": API_KEY},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


def check_once(imap):
    imap.select("INBOX")
    # 查近 2 天内主题带前缀的全部邮件，用本地 UID 记录去重（不依赖未读标记）
    since = (datetime.date.today() - datetime.timedelta(days=2)).strftime("%d-%b-%Y")
    typ, data = imap.uid("SEARCH", None, "SINCE", since, "SUBJECT", f'"{SUBJECT_PREFIX}"')
    if typ != "OK":
        return
    changed = False
    for uid in data[0].split():
        uid_s = uid.decode()
        if uid_s in seen_uids:
            continue
        typ, fetched = imap.uid("FETCH", uid, "(RFC822)")
        if typ != "OK" or not fetched or not fetched[0]:
            continue
        msg = email.message_from_bytes(fetched[0][1])
        sender = decode_header_value(msg.get("From", "")).lower()
        if EXPECTED_FROM not in sender:
            print(f"跳过非预期发件人：{sender[:60]}", flush=True)
            imap.uid("STORE", uid, "+FLAGS", "\\Seen")
            seen_uids.add(uid_s)
            changed = True
            continue
        subject = decode_header_value(msg.get("Subject", ""))
        body = get_text_body(msg).strip()
        # 标题只用于日志，不推送到 QQ（用户要求推送只显示正文）
        title = subject[len(SUBJECT_PREFIX):].strip(" -")
        content = body
        if not content:
            imap.uid("STORE", uid, "+FLAGS", "\\Seen")
            seen_uids.add(uid_s)
            changed = True
            continue
        try:
            status = push_to_qq(content[:1500])
            print(f"已转发到 QQ（relay={status}）：{title[:40]}", flush=True)
        except Exception as e:
            # 转发失败不记入已处理，下次轮询重试
            print(f"转发失败，稍后重试：{e}", flush=True)
            continue
        seen_uids.add(uid_s)
        changed = True
        imap.uid("STORE", uid, "+FLAGS", "\\Seen")
    if changed:
        save_seen(seen_uids)


def main():
    while True:
        try:
            print("连接 QQ邮箱 IMAP…", flush=True)
            imap = imaplib.IMAP4_SSL("imap.qq.com", 993)
            imap.login(MAIL_USER, MAIL_AUTHCODE)
            print("登录成功，开始轮询", flush=True)
            fails = 0
            while True:
                try:
                    check_once(imap)
                    fails = 0
                except Exception as e:
                    fails += 1
                    print(f"轮询出错（{fails}）：{e}", flush=True)
                    if fails >= 3:
                        # 连接层异常（Broken pipe 等）：QQ 会掐空闲连接，
                        # 必须跳出内层循环重建连接，否则拿死连接空转
                        print("连续失败，重建 IMAP 连接…", flush=True)
                        try:
                            imap.logout()
                        except Exception:
                            pass
                        break
                time.sleep(POLL_INTERVAL)
        except Exception as e:
            print(f"IMAP 连接断开（{e}），10 秒后重连…", flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
