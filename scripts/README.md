# scripts/paa-tunnel.sh —— 面板 SSH 隧道守护脚本

本机（你的电脑）运行的脚本：**电脑重启 / 休眠唤醒 / 锁屏断网 / 网络切换导致 SSH 隧道断开后，自动重连**，保证这些地址随时可打开：

- PAA 数据面板：<http://127.0.0.1:8765/>
- AstrBot WebUI：<http://127.0.0.1:16185/>

## 它做了什么

脚本把两条隧道交给一个「看门狗」管理（每条隧道独立 `ssh -L`）：

1. 每 10 秒对 `127.0.0.1:8765/api/health` 和 `127.0.0.1:16185/` 做健康检查；
2. 发现失败 → 杀掉失效的 ssh 进程并立即重连；重连失败按 3s → 6s → 12s → … → 60s 退避重试；
3. ssh 探活参数：`ServerAliveInterval=15` + `ServerAliveCountMax=3`（约 45 秒内可发现半死连接）、`ExitOnForwardFailure=yes`、`ConnectTimeout=10`、`BatchMode=yes`（绝不弹交互）；
4. 检测到休眠/挂起恢复（时间跳变 > 60s）立即强制重检；
5. 单实例运行（`flock`），日志轮转，`systemd --user` 服务 `Restart=always` 兜底；
6. 支持 `loginctl enable-linger`：**未登录桌面也能在开机后自动建隧道**（安装时已尝试开启；用 `loginctl show-user $USER -p Linger` 查看）。

## 快速开始

```bash
cd ~/Projects/paa-demo/plugin/scripts

./paa-tunnel.sh --help     # 查看完整使用说明（命令/配置/排障/示例）
./paa-tunnel.sh install    # 安装 systemd 用户服务 + 开机自启 + 立即启动（推荐）
./paa-tunnel.sh status     # 查看服务状态、两条隧道健康状态、最近日志
./paa-tunnel.sh logs -f    # 持续跟踪日志
./paa-tunnel.sh stop       # 停止
./paa-tunnel.sh start      # 再启动
./paa-tunnel.sh uninstall  # 卸载 systemd 服务与自启动
```

`install` 的产物：

- `~/.config/systemd/user/paa-tunnel.service`（`systemctl --user enable --now paa-tunnel`，`Restart=always`）
- 状态/日志：`~/.local/state/paa-tunnel/`（`tunnel.log`、`watchdog.pid`、`tunnel-<端口>.pid`）
- 可选配置：`~/.config/paa-tunnel.env`（不用改脚本，覆盖 `PAA_ECS_HOST` / `PAA_SSH_KEY` / 端口等，参见脚本头部注释）

## 已验证场景（2026-11-18 本机实测）

| 场景 | 操作 | 结果 |
|---|---|---|
| 手工隧道被结束 | `kill` 两条原有 ssh | 约 10s 内两条均自动重建，面板 200 |
| 隧道进程被强杀 | `kill -9` 看门狗管理的 ssh | 约 10s 内重建（日志有 WARN → INFO 记录） |
| systemd 重启 | `systemctl --user restart paa-tunnel` | 服务 active，两条隧道重新建立 |
| 服务停止/启动 | `./paa-tunnel.sh stop && ./paa-tunnel.sh start` | 正常，启动后自动恢复 |
| 重启电脑 | 服务 `enabled` + `Linger=yes` | 开机（甚至未登录）即由 systemd 拉起看门狗 |

## 前置条件（已满足）

- 本机 `~/.ssh/id_rsa` 已加入 ECS `root` 的 `authorized_keys`；ECS `39.103.56.192:22` 可达；
- 服务器上 `paa-panel.service` 已 `enabled`，面板监听 ECS `127.0.0.1:8765`；
- `~/.ssh/config` 里也有 `Host paa-ecs`（含两条 `LocalForward`），想手动开会话时 `ssh paa-ecs` 即可。

## 排障

```bash
systemctl --user status paa-tunnel --no-pager
journalctl --user -u paa-tunnel -n 50 --no-pager
tail -f ~/.local/state/paa-tunnel/tunnel.log
./paa-tunnel.sh status
```

- 端口被别的手工 ssh 占用：看门狗会识别到「健康」并让位，不会互相抢端口；
- 服务器面板本身挂了：先 `ssh paa-ecs 'systemctl status paa-panel'`（隧道重连不能修复服务端故障）；
- 换服务器/端口：编辑 `~/.config/paa-tunnel.env` 后 `./paa-tunnel.sh restart`。
