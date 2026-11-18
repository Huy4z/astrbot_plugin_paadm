#!/usr/bin/env bash
# =============================================================================
# paa-tunnel.sh —— PAA 数据面板 SSH 隧道守护脚本（本机运行）
#
# 目标：电脑重启 / 休眠唤醒 / 锁屏断网 / 网络切换导致 SSH 隧道断开后，
#       自动重新建立到 ECS 的本地端口转发，让 http://127.0.0.1:8765/ 始终可用。
#
# 隧道内容（两条独立的 ssh 进程，任一断开只重连该条）：
#   127.0.0.1:8765  -> ECS 127.0.0.1:8765   （PAA 数据面板）
#   127.0.0.1:16185 -> ECS 127.0.0.1:6185   （AstrBot WebUI，附带）
#
# 用法：
#   ./paa-tunnel.sh install    # 安装为 systemd 用户服务（开机/登录自启）+ 立即启动
#   ./paa-tunnel.sh start      # 后台启动看门狗（不装 systemd）
#   ./paa-tunnel.sh status     # 查看服务/隧道健康状态
#   ./paa-tunnel.sh logs       # 查看日志（-f 持续跟踪）
#   ./paa-tunnel.sh stop       # 停止
#   ./paa-tunnel.sh uninstall  # 卸载 systemd 用户服务
#   ./paa-tunnel.sh run        # 前台运行看门狗（systemd 用，一般不用手动执行）
#
# 可覆盖的环境变量（也可写进 ~/.config/paa-tunnel.env）：
#   PAA_ECS_HOST / PAA_SSH_USER / PAA_SSH_KEY
#   PAA_PANEL_PORT / PAA_PANEL_REMOTE_PORT / PAA_WEBUI_PORT / PAA_WEBUI_REMOTE_PORT
#   PAA_CHECK_INTERVAL（健康检查间隔秒数，默认 10）
# =============================================================================
set -u

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SELF")" && pwd)"
APP_NAME="paa-tunnel"

# 可选的本机配置文件（不放进仓库，避免泄露个人路径）
if [[ -f "${XDG_CONFIG_HOME:-$HOME/.config}/paa-tunnel.env" ]]; then
    # shellcheck source=/dev/null
    source "${XDG_CONFIG_HOME:-$HOME/.config}/paa-tunnel.env"
fi

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/$APP_NAME"
LOG_FILE="$STATE_DIR/tunnel.log"
LOCK_FILE="$STATE_DIR/watchdog.lock"
PID_FILE="$STATE_DIR/watchdog.pid"

UNIT_NAME="paa-tunnel.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_FILE="$UNIT_DIR/$UNIT_NAME"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
AUTOSTART_FILE="$AUTOSTART_DIR/paa-tunnel.desktop"

ECS_HOST="${PAA_ECS_HOST:-39.103.56.192}"
SSH_USER="${PAA_SSH_USER:-root}"
SSH_KEY="${PAA_SSH_KEY:-$HOME/.ssh/id_rsa}"
PANEL_LOCAL_PORT="${PAA_PANEL_PORT:-8765}"
PANEL_REMOTE_PORT="${PAA_PANEL_REMOTE_PORT:-8765}"
WEBUI_LOCAL_PORT="${PAA_WEBUI_PORT:-16185}"
WEBUI_REMOTE_PORT="${PAA_WEBUI_REMOTE_PORT:-6185}"
CHECK_INTERVAL="${PAA_CHECK_INTERVAL:-10}"
CONNECT_TIMEOUT="${PAA_CONNECT_TIMEOUT:-10}"

SPEC_NAMES=(panel webui)
SPEC_LOCAL=("$PANEL_LOCAL_PORT" "$WEBUI_LOCAL_PORT")
SPEC_REMOTE=("$PANEL_REMOTE_PORT" "$WEBUI_REMOTE_PORT")
SPEC_LABEL=("数据面板 http://127.0.0.1:$PANEL_LOCAL_PORT/" "AstrBot WebUI http://127.0.0.1:$WEBUI_LOCAL_PORT/")

mkdir -p "$STATE_DIR" 2>/dev/null || true
chmod 700 "$STATE_DIR" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
log() {
    local level="$1"
    shift
    printf '%s [%s] %s\n' "$(date '+%F %T')" "$level" "$*" >>"$LOG_FILE" 2>/dev/null || true
}

say() { printf '%s\n' "$*"; }

rotate_log() {
    if [[ -f "$LOG_FILE" ]] && [[ "$(stat -c %s "$LOG_FILE" 2>/dev/null || echo 0)" -gt 1048576 ]]; then
        mv -f "$LOG_FILE" "$LOG_FILE.1" 2>/dev/null || true
    fi
}

port_healthy() {
    local name="$1"
    case "$name" in
        panel) curl -fsS --max-time 5 -o /dev/null "http://127.0.0.1:$PANEL_LOCAL_PORT/api/health" 2>/dev/null ;;
        webui) curl -fsS --max-time 5 -o /dev/null "http://127.0.0.1:$WEBUI_LOCAL_PORT/" 2>/dev/null ;;
        *) return 1 ;;
    esac
}

tunnel_pidfile() { printf '%s/tunnel-%s.pid' "$STATE_DIR" "$1"; }
tunnel_errlog() { printf '%s/tunnel-%s.err.log' "$STATE_DIR" "$1"; }

read_pid() {
    local file="$1"
    [[ -f "$file" ]] || return 1
    local pid
    pid="$(cat "$file" 2>/dev/null || true)"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    printf '%s' "$pid"
}

kill_child() {
    local lport="$1"
    local pidfile pid
    pidfile="$(tunnel_pidfile "$lport")"
    if pid="$(read_pid "$pidfile")"; then
        kill -TERM "$pid" 2>/dev/null || true
        for _ in $(seq 1 20); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.2
        done
        kill -KILL "$pid" 2>/dev/null || true
        log INFO "已结束旧隧道进程 $pid（本地端口 $lport）"
    fi
    rm -f "$pidfile"
}

kill_children() {
    local i
    for i in "${!SPEC_LOCAL[@]}"; do
        kill_child "${SPEC_LOCAL[$i]}"
    done
}

# 建立一条 ssh -L 隧道并等待其健康
start_child() {
    local name="$1" lport="$2" rport="$3"
    local pidfile errlog pid ok=0
    pidfile="$(tunnel_pidfile "$lport")"
    errlog="$(tunnel_errlog "$lport")"
    : >"$errlog" 2>/dev/null || true

    # -N 不执行远端命令；ExitOnForwardFailure 让端口占用时立即失败；
    # ServerAlive* 让 ssh 自己发现半死连接；BatchMode 保证绝不交互卡住。
    ssh -i "$SSH_KEY" \
        -o BatchMode=yes \
        -o StrictHostKeyChecking=accept-new \
        -o ConnectTimeout="$CONNECT_TIMEOUT" \
        -o ServerAliveInterval=15 \
        -o ServerAliveCountMax=3 \
        -o TCPKeepAlive=yes \
        -o ExitOnForwardFailure=yes \
        -N -L "$lport:127.0.0.1:$rport" "$SSH_USER@$ECS_HOST" \
        >>"$errlog" 2>&1 &
    pid=$!
    printf '%s' "$pid" >"$pidfile"

    for _ in $(seq 1 25); do
        if ! kill -0 "$pid" 2>/dev/null; then
            log ERROR "$name 隧道进程提前退出：$(tail -n 3 "$errlog" 2>/dev/null | tr '\n' ' ')"
            rm -f "$pidfile"
            return 1
        fi
        if port_healthy "$name"; then
            ok=1
            break
        fi
        sleep 1
    done

    if ((ok == 1)); then
        log INFO "$name 隧道已就绪：127.0.0.1:$lport -> $SSH_USER@$ECS_HOST:127.0.0.1:$rport (pid $pid)"
        return 0
    fi

    log ERROR "$name 隧道 25s 内未通过健康检查，放弃本次进程 pid=$pid：$(tail -n 3 "$errlog" 2>/dev/null | tr '\n' ' ')"
    kill_child "$lport"
    return 1
}

# ---------------------------------------------------------------------------
# 看门狗主循环（前台；由 systemd 或 start 的 setsid 拉起）
# ---------------------------------------------------------------------------
cmd_run() {
    mkdir -p "$STATE_DIR"
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        say "另一个看门狗实例已在运行（锁：$LOCK_FILE），退出。"
        exit 0
    fi
    echo $$ >"$PID_FILE"
    rotate_log

    local child_pids=()
    cleanup() {
        trap - EXIT INT TERM
        log INFO "看门狗收到退出信号，清理隧道进程"
        local i
        for i in "${!SPEC_LOCAL[@]}"; do
            kill_child "${SPEC_LOCAL[$i]}"
        done
        rm -f "$PID_FILE"
        exit 0
    }
    trap cleanup EXIT INT TERM

    log INFO "看门狗启动：pid=$$ host=$SSH_USER@$ECS_HOST 端口 ${PANEL_LOCAL_PORT}/${WEBUI_LOCAL_PORT}，检查间隔 ${CHECK_INTERVAL}s"

    local -A backoff=()
    local -A next_try=()
    local i
    for i in "${!SPEC_NAMES[@]}"; do
        backoff[${SPEC_NAMES[$i]}]=0
        next_try[${SPEC_NAMES[$i]}]=0
    done

    local last_tick
    last_tick=$(date +%s)

    while :; do
        local now gap
        now=$(date +%s)
        gap=$((now - last_tick))
        last_tick=$now
        # 休眠/挂起后 monotonic 不跳、墙钟跳；间隔异常大说明刚被唤醒，立即重检
        if ((gap > 60)); then
            log WARN "检测到休眠/挂起恢复（时间跳变 ${gap}s），立即重连检查"
            for i in "${!SPEC_NAMES[@]}"; do
                next_try[${SPEC_NAMES[$i]}]=0
                backoff[${SPEC_NAMES[$i]}]=0
            done
        fi

        for i in "${!SPEC_NAMES[@]}"; do
            local name="${SPEC_NAMES[$i]}"
            local lport="${SPEC_LOCAL[$i]}"
            local rport="${SPEC_REMOTE[$i]}"
            local label="${SPEC_LABEL[$i]}"

            if port_healthy "$name"; then
                # 健康：顺手清理已失效的 pid 文件（可能是后加入的其它隧道在工作）
                local pidfile
                pidfile="$(tunnel_pidfile "$lport")"
                if [[ -f "$pidfile" ]] && ! read_pid "$pidfile" >/dev/null; then
                    rm -f "$pidfile"
                fi
                backoff[$name]=0
                next_try[$name]=0
                continue
            fi

            if ((now < ${next_try[$name]:-0})); then
                continue
            fi

            log WARN "$name 健康检查失败（$label），准备重连"
            kill_child "$lport"
            if start_child "$name" "$lport" "$rport"; then
                backoff[$name]=0
                next_try[$name]=0
            else
                local b=${backoff[$name]:-0}
                if ((b == 0)); then b=3; else b=$((b * 2)); fi
                ((b > 60)) && b=60
                backoff[$name]=$b
                next_try[$name]=$(( $(date +%s) + b ))
                log ERROR "$name 重连失败，${b}s 后重试"
            fi
        done

        sleep "$CHECK_INTERVAL" &
        wait $! 2>/dev/null || true
    done
}

# ---------------------------------------------------------------------------
# 启停/状态
# ---------------------------------------------------------------------------
unit_installed() { [[ -f "$UNIT_FILE" ]]; }
unit_active() { systemctl --user is-active --quiet "$UNIT_NAME" 2>/dev/null; }

cmd_start() {
    mkdir -p "$STATE_DIR"
    if unit_installed; then
        if unit_active; then
            say "systemd 用户服务已在运行（$UNIT_NAME）。"
        else
            systemctl --user start "$UNIT_NAME" 2>/dev/null || true
            say "已通过 systemd 用户服务启动（$UNIT_NAME）。"
            sleep 1
        fi
        cmd_status
        return 0
    fi
    if ! flock -n "$LOCK_FILE" true 2>/dev/null; then
        say "看门狗已在后台运行中（锁已被占用）。"
        cmd_status
        return 0
    fi
    rotate_log
    # setsid + nohup：脱离当前终端/会话，锁屏或关闭终端都不会结束它
    setsid nohup "$SELF" run >>"$LOG_FILE" 2>&1 < /dev/null &
    disown 2>/dev/null || true
    sleep 2
    cmd_status
}

cmd_stop() {
    if unit_installed && unit_active; then
        systemctl --user stop "$UNIT_NAME" 2>/dev/null || true
        say "已停止 systemd 用户服务 $UNIT_NAME。"
    fi
    local pidfile_pid=""
    if [[ -f "$PID_FILE" ]]; then
        pidfile_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    fi
    if [[ "$pidfile_pid" =~ ^[0-9]+$ ]] && kill -0 "$pidfile_pid" 2>/dev/null; then
        kill -TERM "$pidfile_pid" 2>/dev/null || true
        sleep 1
        kill -KILL "$pidfile_pid" 2>/dev/null || true
        say "已停止看门狗进程 $pidfile_pid。"
    fi
    rm -f "$PID_FILE"
    kill_children
    say "隧道已停止（如需恢复：$SELF start 或 $SELF install）。"
}

cmd_status() {
    local rc=0
    say "== PAA 隧道状态 =="
    if unit_installed; then
        local state mainpid
        state="$(systemctl --user is-active "$UNIT_NAME" 2>/dev/null || true)"
        mainpid="$(systemctl --user show -p MainPID --value "$UNIT_NAME" 2>/dev/null || true)"
        say "systemd 用户服务：${state:-unknown}（MainPID=${mainpid:-?}）"
    else
        say "systemd 用户服务：未安装（可用 '$SELF install' 安装开机自启）"
    fi
    if pid="$(read_pid "$PID_FILE")"; then
        say "看门狗进程：$pid（日志：$LOG_FILE）"
    else
        say "看门狗进程：未运行"
    fi
    local i
    for i in "${!SPEC_NAMES[@]}"; do
        local name="${SPEC_NAMES[$i]}"
        local lport="${SPEC_LOCAL[$i]}"
        local label="${SPEC_LABEL[$i]}"
        local tpid
        tpid="$(read_pid "$(tunnel_pidfile "$lport")" 2>/dev/null || true)"
        if port_healthy "$name"; then
            say "  [OK]   $label  (本机端口 $lport，隧道 pid=${tpid:-外部进程})"
        else
            say "  [DOWN] $label  (本机端口 $lport 不可用)"
            rc=1
        fi
    done
    if [[ -f "$LOG_FILE" ]]; then
        say "-- 最近日志 --"
        tail -n 6 "$LOG_FILE" 2>/dev/null | sed 's/^/  /'
    fi
    return "$rc"
}

cmd_logs() {
    [[ -f "$LOG_FILE" ]] || { say "还没有日志（$LOG_FILE）。"; return 1; }
    if [[ "${1:-}" == "-f" ]]; then
        tail -n 50 -f "$LOG_FILE"
    else
        tail -n "${1:-50}" "$LOG_FILE"
    fi
}

# ---------------------------------------------------------------------------
# 安装/卸载开机自启（systemd 用户服务，失败则退回 XDG autostart）
# ---------------------------------------------------------------------------
write_unit_file() {
    mkdir -p "$UNIT_DIR"
    cat >"$UNIT_FILE" <<EOF
[Unit]
Description=PAA panel SSH tunnel watchdog (localhost:$PANEL_LOCAL_PORT, localhost:$WEBUI_LOCAL_PORT)
Documentation=file:$SELF
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=$SELF run
Restart=always
RestartSec=5
Environment=PAA_ECS_HOST=$ECS_HOST
Environment=PAA_SSH_USER=$SSH_USER
Environment=PAA_SSH_KEY=$SSH_KEY
Environment=PAA_PANEL_PORT=$PANEL_LOCAL_PORT
Environment=PAA_PANEL_REMOTE_PORT=$PANEL_REMOTE_PORT
Environment=PAA_WEBUI_PORT=$WEBUI_LOCAL_PORT
Environment=PAA_WEBUI_REMOTE_PORT=$WEBUI_REMOTE_PORT

[Install]
WantedBy=default.target
EOF
}

write_autostart_file() {
    mkdir -p "$AUTOSTART_DIR"
    cat >"$AUTOSTART_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=PAA Panel SSH Tunnel
Comment=Keep 127.0.0.1:$PANEL_LOCAL_PORT panel tunnel alive
Exec=$SELF start
Terminal=false
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF
}

cmd_install() {
    mkdir -p "$STATE_DIR"
    local installed_ok=0
    if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
        write_unit_file
        if systemctl --user daemon-reload >/dev/null 2>&1 &&
           systemctl --user enable --now "$UNIT_NAME" >/dev/null 2>&1; then
            installed_ok=1
            say "已安装 systemd 用户服务：$UNIT_FILE"
            say "  - 登录桌面后自动启动；异常退出自动 Restart（Restart=always）"
        else
            say "systemd 用户服务安装失败，将退回 XDG autostart。"
        fi
    fi
    if ((installed_ok == 0)); then
        write_autostart_file
        say "已写入 XDG 自启动项：$AUTOSTART_FILE（登录桌面后执行 '$SELF start'）"
        cmd_start
    fi
    # 可选：允许未登录也启动（需要 polkit 授权；失败不影响登录后自启）
    if command -v loginctl >/dev/null 2>&1; then
        if ! loginctl enable-linger "$USER" >/dev/null 2>&1; then
            say "提示：如需在“未登录桌面”时也保持隧道，可手动执行（需要管理员授权）："
            say "      sudo loginctl enable-linger $USER"
        fi
    fi
    sleep 1
    cmd_status
}

cmd_uninstall() {
    if unit_installed; then
        systemctl --user disable --now "$UNIT_NAME" >/dev/null 2>&1 || true
        rm -f "$UNIT_FILE"
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        say "已移除 systemd 用户服务 $UNIT_NAME"
    fi
    [[ -f "$AUTOSTART_FILE" ]] && rm -f "$AUTOSTART_FILE" && say "已移除 XDG 自启动项"
    cmd_stop
}

usage() {
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 && !/^#/ { exit }' "$SELF"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
case "${1:-start}" in
    run) cmd_run ;;
    start) cmd_start ;;
    stop) cmd_stop ;;
    restart) cmd_stop; sleep 1; cmd_start ;;
    status) cmd_status ;;
    logs) shift; cmd_logs "$@" ;;
    install) cmd_install ;;
    uninstall) cmd_uninstall ;;
    help | -h | --help) usage ;;
    *)
        say "未知命令：$1"
        usage
        exit 2
        ;;
esac
