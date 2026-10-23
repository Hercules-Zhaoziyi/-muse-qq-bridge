#!/bin/bash
# QQ 邮件桥服务守护脚本（1Panel 计划任务调用，执行周期设为每 5 分钟）
# 职责：三个脚本谁没跑就拉起来；中转进程活着但端口没监听（假死）就杀掉等下轮拉起。
# 密钥统一从 /opt/qq-push/.env 读取（chmod 600）。
#
# 实现说明：刻意不用 pgrep/pkill。1Panel 以 bash -c 方式执行本脚本时，
# 脚本全文都会出现在自己进程的命令行里，pgrep -f 按完整命令行匹配必定
# 误伤自己（启动时误判"已在运行"，pkill 时甚至把任务自己的 shell 杀掉）。
# 改用 pid 文件记录，kill -0 只按数字判断进程存活，不做任何字符串匹配。

ENV_FILE=/opt/qq-push/.env
WORKDIR=/opt/qq-push
LOG=$WORKDIR/watchdog.log

[ -f "$ENV_FILE" ] || exit 1
set -a
source "$ENV_FILE"
set +a
cd "$WORKDIR" || exit 1

# 进程是否还在：pid 文件存在、/proc 下有该 pid、且命令行里确实是目标脚本
is_running() { # $1 = pidfile, $2 = 脚本名(不含.py)
  pidfile=$1
  name=$2
  [ -f "$pidfile" ] || return 1
  pid=$(cat "$pidfile")
  [ -n "$pid" ] || return 1
  [ -d "/proc/$pid" ] || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "$name.py" || return 1
  return 0
}

start_one() { # $1 = 脚本名(不含.py)
  name=$1
  pidfile=$WORKDIR/$name.pid
  if is_running "$pidfile" "$name"; then
    return 0
  fi
  rm -f "$pidfile"
  nohup python3 "$WORKDIR/$name.py" >> "$WORKDIR/$name.log" 2>&1 &
  echo $! > "$pidfile"
  echo "$(date '+%F %T') started $name.py (pid $!)" >> "$LOG"
  # 刚拉起来的中转还没开始监听端口，本轮跳过假死检查（否则必被误杀）
  if [ "$name" = "qq_official_relay" ]; then
    relay_just_started=1
  fi
}

relay_just_started=0
start_one qq_official_relay
start_one mail_poller
start_one qq_event_listener

# 假死检查：relay 进程在，但 8765 没监听 → 杀掉删 pid 文件，下一轮 start_one 会拉起
# （本轮刚拉起来的跳过，给它启动时间）
pidfile=$WORKDIR/qq_official_relay.pid
if [ "$relay_just_started" -eq 0 ] && is_running "$pidfile" qq_official_relay; then
  if ! curl -sf --max-time 5 http://127.0.0.1:8765/health > /dev/null 2>&1; then
    pid=$(cat "$pidfile")
    kill "$pid" 2>/dev/null
    rm -f "$pidfile"
    echo "$(date '+%F %T') killed wedged qq_official_relay (pid $pid)" >> "$LOG"
  fi
fi

echo "$(date '+%F %T') watchdog tick ok" >> "$LOG"
