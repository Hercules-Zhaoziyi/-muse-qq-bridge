> 突然有个想法：能不能让 Muse 接入 QQ Bot，把它发送的重要信息和贴士同步到我日常使用的 QQ 里，让接收消息变得更方便。

![对话截图](https://bee-reg-ab.imagency.cn/mr/6957/26/6ac4e0765df02.png)

目标：让 Muse（或其他 AI 助手）与用户的 QQ 双向通信——助手 → QQ 推送重要消息；用户在 QQ 上发消息 → 助手能收到并回复。也不需要任何入站端口。

---

## 1. 方案总览

### 为什么是邮件桥

实测中常见的网络约束（2026-10 实测）：

- Muse 沙箱的出站流量到用户服务器被透明劫持，直连不通。
- 用户服务器在中国大陆，访问不了 ntfy.sh 等国外中转服务。
- 双方都只有"出站"能上网，而**邮件（Gmail / QQ邮箱）两边都通**。

所以用邮件做传输层：两边都只做"发邮件 / 收邮件"，不依赖任何直连。

### 架构

```
正向（助手 → QQ）：
Muse --Gmail发[MusePush]邮件--> 用户QQ邮箱 --IMAP轮询(30s)--> mail_poller.py
  --POST http://127.0.0.1:8765/push--> qq_official_relay.py --QQ官方API--> QQ私聊

反向（QQ → 助手）：
QQ私聊 --QQ网关长连接--> qq_event_listener.py --SMTP发[MuseCmd]邮件--> 助手Gmail
  --助手侧轮询(3min)--> 助手处理 --走正向链路--> 回复到QQ
```

### 延迟预期

- 正向：约 1 分钟内到达 QQ。
- 反向：2\~5 分钟（两边轮询叠加），适合留言式聊天，不适合掐秒的急事。

![效果展示](https://bee-reg-ab.imagency.cn/mr/6957/26/6ac4dee74b3ba.jpeg)

---

## 2. 前置条件

1. [**QQ开放平台**](https://q.qq.com/)：拿到 AppID 和 AppSecret（qq.qq.com → 机器人管理）。
![创建机器人](https://bee-reg-ab.imagency.cn/mr/6957/26/6ac4e2486f041.png)
   机器人**不需要上线**——实测离线状态也能发主动消息。
2. **user\_openid**：机器人与用户私聊的 openid。用本包 `get_openid.py`
   连网关，然后用 QQ 给机器人发一句话，从 `C2C_MESSAGE_CREATE` 事件里抓。
    ![获取用户ID](https://bee-reg-ab.imagency.cn/mr/6957/26/6ac4e80b61c24.png)
3. **一台服务器**：Python 3.6+，无第三方依赖（本教程用阿里云 ECS）。
   注意：如果 Python 是 3.6，`http.server.ThreadingHTTPServer` 不可用
   ![教程演示机](https://bee-reg-ab.imagency.cn/mr/6957/26/6ac4e2daa2ec3.png)
   （3.7+ 才有），本包的 `qq_official_relay.py` 已用 `ThreadingMixIn` 兼容写法。
4. **QQ邮箱**：设置 → 账户 → 开启 IMAP/SMTP，用密保手机生成**授权码**
   （第三方客户端登录用授权码，不是 QQ 密码）。
    ![获取授权码](https://bee-reg-ab.imagency.cn/mr/6957/26/6ac4e39f01e3a.png)
5. **助手侧 Gmail**：已接入、可发信。

---

## 3. 放弃过的方案（不用试了）

- 让 AI 直连 QQ 官方 API：Secure Vault 的 connector 每次请求只存一个
     secret，而官方 API 每次要两个以上不同 secret，被拒。
- NapCat 方案：起因是第三方文档称"主动消息每月仅几条"，后证实是误读，
     用户实测官方配额够用，直接走官方 API。
- ntfy.sh 消息桥：用户服务器在中国上不了外网，AI 沙箱也连不上 ntfy，
     双方都不通。

---

## 4. 文件清单（本包内）
下载地址：<a href="http://cccimg.com/down.php/e213de200a614f6a8ca77521155fbe38.zip" target="_blank">qq-bridge-tutorial.zip</a>

### 文件说明

#### `qq_official_relay.py`

- **运行位置**：服务器
- **运行方式**：systemd 常驻
- **作用**：提供本地 HTTP 服务（`127.0.0.1:8765`），通过 `POST /push` 调用 QQ 官方 API 发送私聊消息。

---

#### `mail_poller.py`

- **运行位置**：服务器
- **运行方式**：nohup / systemd
- **作用**：通过 IMAP 轮询 QQ 邮箱，将带有 `[MusePush]` 标记的邮件转发给本地中转服务。

---

#### `qq_event_listener.py`

- **运行位置**：服务器
- **运行方式**：nohup / systemd
- **作用**：保持 QQ 网关连接，接收用户私聊消息，并通过 SMTP 发送带有 `[MuseCmd]` 标记的邮件到助手 Gmail。

---

#### `get_openid.py`

- **运行位置**：任意设备
- **运行方式**：一次性执行
- **作用**：连接 QQ 网关，获取机器人对应用户的 `user_openid`。
![B2D5830A450176D6E608C2BDB01FDC38](https://bee-reg-ab.imagency.cn/mr/6957/26/6ac4e80b61c24.png)
---

#### `qq_push_relay.service`

- **运行位置**：服务器
- **运行方式**：systemd 服务模板
- **作用**：用于配置中转服务的 systemd 启动文件，其中 `<...>` 部分需要替换为实际配置。

所有脚本配置全走**环境变量**，文件里没有任何要填的占位符。

---

## 5. 部署步骤

### 5.1 部署中转服务

1. 把 `qq_official_relay.py` 传到服务器 `/opt/qq-push/`。
2. 按模板写 `/etc/systemd/system/qq_push_relay.service`，填四个环境变量：

```
Environment=QQ_BOT_APPID=<机器人AppID>
Environment=QQ_BOT_SECRET=<机器人AppSecret>
Environment=QQ_USER_OPENID=<get_openid.py 抓到的openid>
Environment=PUSH_API_KEY=<自己编的随机长字符串，如 openssl rand -hex 32>
```

`PUSH_API_KEY` 是轮询脚本调用本地中转时的单密钥，自己编一个完全随机的，
不要从 AppSecret 衍生。

![部署中转服务](https://bee-reg-ab.imagency.cn/mr/6957/26/6ac4e87145ad8.png)

3. 启动并验证：

```
systemctl daemon-reload
systemctl enable --now qq_push_relay
ss -tlnp | grep 8765        # 应看到 0.0.0.0:8765 LISTEN
curl -s http://127.0.0.1:8765/health   # 应返回 ok
```

### 5.2 部署邮件轮询（正向）

1. 把 `mail_poller.py` 传到 `/opt/qq-push/`。
2. 启动：

```
cd /opt/qq-push
QQ_MAIL_USER=<你的QQ邮箱> \
QQ_MAIL_AUTHCODE=<QQ邮箱授权码> \
PUSH_API_KEY=<与中转服务相同的单密钥> \
nohup python3 mail_poller.py > mail_poller.log 2>&1 &
```

3. 看日志确认：`tail mail_poller.log` 应出现"登录成功，开始轮询"。
   （建议之后也做成 systemd 服务开机自启。）

![部署邮件轮询]()

### 5.3 部署事件监听（反向）

1. 把 `qq_event_listener.py` 传到 `/opt/qq-push/`。
2. 启动：

```
cd /opt/qq-push
QQ_BOT_APPID=<机器人AppID> \
QQ_BOT_SECRET=<机器人AppSecret> \
QQ_USER_OPENID=<user_openid> \
QQ_MAIL_USER=<你的QQ邮箱> \
QQ_MAIL_AUTHCODE=<QQ邮箱授权码> \
nohup python3 qq_event_listener.py > qq_event_listener.log 2>&1 &
```

`FORWARD_TO` 环境变量可选，默认发到助手的 Gmail（见 6.1）。

### 5.4 助手侧配置

1. **发件约定**：助手用 Gmail 发 `[MusePush]` 开头的邮件到用户 QQ邮箱。

- 主题：`[MusePush]` 前缀（前缀后的标题**不会**推送到 QQ，只进服务器日志）。
- 正文：纯文本（脚本只取 `text/plain` 部分，纯图片/附件的邮件推不出去）。
- 发件人必须是约定好的 Gmail 地址（轮询脚本校验 `EXPECTED_FROM`）。

2. **轮询&#32;`[MuseCmd]`**：助手侧每 2\~3 分钟查一次 Gmail，
   搜索未读、主题含 `[MuseCmd]`、发件人为用户 QQ邮箱的邮件；
   读完标已读，内容即用户在 QQ 上说的话，回复走正向链路。
   去重要求：不要只依赖未读标记，用本地邮件 ID 记录兜底
   （用户可能在 Gmail 里点开邮件导致标记失效）。

---

## 6. 测试

1. **正向**：助手发一封 `[MusePush] 测试` 邮件 → 1 分钟内 QQ 应收到正文。
2. **反向**：用户在 QQ 给机器人发一句话 → 监听日志出现"已转发到邮箱" →
   助手 Gmail 收到 `[MuseCmd]` → 助手从 QQ 回一句。
3. **手动触发推送**（不用经过助手）：用约定的 Gmail 手动发一封
   `[MusePush]` 邮件到 QQ邮箱，30 秒内 QQ 应收到。

---

## 7. 故障排查

| 现象 | 查哪里 | 常见原因 |
| --- | --- | --- |

| 轮询日志 `Connection refused` | `systemctl status qq_push_relay` / \`ss -tlnp \\ | grep 8765\` | 中转服务没起来 |

| 中转服务反复重启，`ImportError: ThreadingHTTPServer` | `journalctl -u qq_push_relay` | 服务器 Python 3.6，用本包已修复的兼容写法 |
| 轮询登录成功但抓不到邮件 | `tail mail_poller.log` | 主题前缀不对 / 发件人不对 / 邮件 UID 已在本地去重记录里 |
| 邮件到了 QQ邮箱但 QQ 没收到 | 轮询日志是否有"已转发到 QQ" | 中转调官方 API 失败：检查 AppSecret 是否最新（重置后要更新 service 文件并 `daemon-reload` + 重启） |
| 反向：QQ 发了话，Gmail 没收到 | `tail qq_event_listener.log` | 网关没连上 / SMTP 授权码不对 |
| 推送内容带标题 | — | 旧版脚本行为；本包已改为只推正文 |

---

## 8. 降噪（可选）

桥接邮件只是传输层，不需要弹通知。用规则让它们静默，重要邮件不受影响：

- **Gmail**（收 `[MuseCmd]`）：网页版 → 设置 → 过滤器和屏蔽的地址 →
  新建过滤器，主题填 `[MuseCmd]` → 跳过收件箱（存档）+ 标为已读 + 打标签。
- **QQ邮箱**（收 `[MusePush]`）：设置 → 收信规则 → 新建：
  主题包含 `[MusePush]` → 移动到指定文件夹 + 标记为已读。

---

\*整理自 2026-10-06 一次真实打通记录。环境：阿里云 ECS（Alibaba Cloud Linux，
Python 3.6）、QQ开放平台机器人、Gmail + QQ邮箱。\*