# astrbot_plugin_paadm

让 AstrBot 通过本地 CLI [`paadm`](../../paadm) 管理个人事务，并把**每一次 paadm 调用**
记录到 SQLite（`interactions.db`），供 HTML 面板展示。

- 命令：`/paa task|schedule|reminder|tx|note|user|raw ...`（QQ / WebChat 等平台通用）
- LLM 工具：`@filter.llm_tool(name="paadm")`，`action + args_json` 通用入口
- 每次实际调用（成功 / 业务失败 / 超时 / 异常）都会写一条交互日志
- 纯 Python 标准库实现，**无第三方依赖**；所有 handler 都定义在 `main.py`

---

## 1. 部署形态（默认配置已匹配）

| 内容 | 容器内路径 | 宿主机路径 |
|---|---|---|
| 插件目录 | `/AstrBot/data/plugins/astrbot_plugin_paadm` | `/opt/astrbot-new/data/plugins/astrbot_plugin_paadm` |
| paadm 可执行文件 | `/AstrBot/data/paa/bin/paadm` | `/opt/astrbot-new/data/paa/bin/paadm` |
| paadm 业务库 | `/AstrBot/data/paa/paa.db` | `/opt/astrbot-new/data/paa/paa.db` |
| 交互日志库 | `/AstrBot/data/paa/interactions.db` | `/opt/astrbot-new/data/paa/interactions.db` |

paadm 的数据库路径通过环境变量 `PAADM_DB_PATH` 传给子进程（不依赖 cwd，
也不在命令里硬编码 `--db`）。

---

## 2. 安装

1. 把整个 `astrbot_plugin_paadm/` 目录放到 AstrBot 的插件目录：

   ```text
   /AstrBot/data/plugins/astrbot_plugin_paadm/
   ├── main.py
   ├── command_router.py
   ├── paadm_client.py
   ├── interaction_store.py
   ├── metadata.yaml
   ├── _conf_schema.json
   ├── requirements.txt
   ├── README.md
   └── tests/
   ```

2. 确认容器内 `paadm` 可执行（见「排障」）。
3. 重启 AstrBot 容器（或在 WebUI 插件管理里重新加载插件）：

   ```bash
   docker restart astrbot-new
   ```

4. 在 AstrBot WebUI 的插件配置页核对 `paadm_bin` / `paadm_db` /
   `interactions_db`，保存后重新加载插件。
5. 私聊机器人发送 `/paa help` 验证；日志里应出现
   `[paadm] v1.0.0 已加载` 和两次启动自检（version / init）。

> 本插件只支持 AstrBot `>=4.29,<5`（已在 4.29.4 + Python 3.12 容器验证接口形态）。

---

## 3. 配置项（`_conf_schema.json`）

| key | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `paadm_bin` | string | `/AstrBot/data/paa/bin/paadm` | paadm 可执行文件路径 |
| `paadm_db` | string | `/AstrBot/data/paa/paa.db` | 业务库；以 `PAADM_DB_PATH` 传给子进程 |
| `interactions_db` | string | `/AstrBot/data/paa/interactions.db` | 交互日志库；目录不存在会自动创建 |
| `timeout_seconds` | int | `15` | 单次 paadm 调用超时（秒） |
| `reply_max_rows` | int | `20` | 列表回复最多显示行数 |
| `log_stdout_chars` | int | `4000` | 日志里 stdout/stderr 保留字符数 |
| `provider_override` | string | `""` | 平台 provider 覆盖（空 = 自动映射） |

provider 自动映射：`aiocqhttp` / `napcat` / `onebot` / `qq_official` → `qq`，
`webchat` → `webchat`，其它平台名小写兜底；都经过 `paadm user resolve` 幂等映射。

---

## 4. 命令清单

时间统一要求**带时区偏移**的 ISO 8601，例如 `2026-11-17T23:59:00+08:00`；
金额单位是**元**（`18.50`），插件自动换算成 paadm 需要的整数人民币分（`1850`）。

### 待办

```text
/paa task add <标题> [--due ISO] [--priority N] [--desc 描述]
/paa task list [--status pending|completed|cancelled] [--limit N]
/paa task done <id>
/paa task del <id>
```

### 日程

```text
/paa schedule add <标题> --start ISO --end ISO [--location 地点] [--all-day 0|1]
/paa schedule list [--from ISO] [--to ISO] [--limit N]
```

### 提醒

```text
/paa reminder add <内容> --at ISO [--item <事项id>]
/paa reminder list [--status pending|triggered|cancelled] [--limit N]
```

### 收支

```text
/paa tx add --type expense|income --amount 18.50 --category food [--desc 备注] [--at ISO]
/paa tx list [--from ISO] [--to ISO]
/paa tx sum [--from ISO] [--to ISO]
```

`--at` 省略时默认当前时间（+08:00）。

### 笔记

```text
/paa note add <内容...> [--title 标题]
/paa note list [--q 关键词] [--limit N]
/paa note get <id>
```

### 其它

```text
/paa help                       中文帮助（/paa 无参数同效）
/paa user                       查看当前发送者对应的 paadm 用户（id/provider/external id）
/paa raw <paadm 参数...>        调试透传（仍自动加 --json，并写交互日志）
```

参数支持 `--flag value` 与 `--flag=value` 两种写法；含空格的标题请加引号
（`/paa task add "写 周报"`）。错误提示面向用户，不会把栈信息发给聊天。
单条回复总长截断到约 1500 字符，列表最多 `reply_max_rows` 行。

---

## 5. LLM 工具

```python
@filter.llm_tool(name="paadm")
async def paadm_tool(self, event, action: str, args_json: str = "") -> str
```

`args_json` 是 **JSON 对象字符串**，键为 paadm 的 snake_case 参数名：
`title` / `description` / `priority` / `due_at` / `status` / `start_at` /
`end_at` / `location` / `all_day` / `content` / `remind_at` / `item_id` /
`type` / `amount` / `category` / `occurred_at` / `keyword` / `from` / `to` /
`limit` / `id` 等。

支持 action：

| action | 必填字段 | 备注 |
|---|---|---|
| `task.create` | title | 可选 description / priority / due_at |
| `task.get` / `task.complete` / `task.cancel` / `task.delete` | id | |
| `task.list` | - | 可 status / priority / due_from / due_to / keyword / limit |
| `schedule.create` | title, start_at | 可选 description / end_at / location / all_day |
| `schedule.get` / `schedule.delete` | id | |
| `schedule.list` | - | 可 from / to / keyword / limit |
| `reminder.create` | content, remind_at | 可选 item_id |
| `reminder.get` / `reminder.cancel` / `reminder.delete` | id | |
| `reminder.list` | - | 可 status / from / to / item_id / limit |
| `transaction.create` | type, amount | **amount 是整数人民币分**；省略 occurred_at 默认当前时间 |
| `transaction.get` / `transaction.delete` | id | |
| `transaction.list` | - | 可 type / category / from / to / limit |
| `transaction.summary` | - | 可 type / category / from / to |
| `note.create` | content | 可选 title |
| `note.get` / `note.delete` | id | |
| `note.list` | - | 可 keyword / from / to / limit |
| `user.resolve` | provider, external_user_id | 不影响当前用户 |
| `user.get` | id | |

返回一个 JSON 字符串（结果过长时退化为 `data_preview`）：

```json
{"success": true, "data": {...}, "error": null}
{"success": false, "data": null, "error": {"code": "TASK_NOT_FOUND", "message": "..."}}
```

示例 prompt（LLM 侧会自动填参数）：

```json
action: "transaction.create"
args_json: {"type": "expense", "amount": 1850, "category": "food", "description": "午饭"}
```

---

## 6. 交互日志 `interactions.db`

纯 stdlib `sqlite3`，WAL + `busy_timeout=5000`，启动时自动建表（幂等）：

```sql
CREATE TABLE IF NOT EXISTS interactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,              -- ISO8601 本地时间 +08:00
  source TEXT NOT NULL,          -- 'command' | 'llm_tool' | 'startup'
  platform TEXT,
  sender_id TEXT,
  paadm_user_id TEXT,
  action TEXT NOT NULL,          -- 如 task.create / user.resolve / version
  argv TEXT,                     -- JSON 数组（含 paadm 二进制路径）
  exit_code INTEGER,
  success INTEGER NOT NULL,      -- 0/1
  duration_ms INTEGER,
  error_code TEXT,
  stdout TEXT,                   -- 按 log_stdout_chars 截断
  stderr TEXT                    -- 同上
);
CREATE INDEX IF NOT EXISTS idx_interactions_ts ON interactions(ts);
CREATE INDEX IF NOT EXISTS idx_interactions_action ON interactions(action);
```

写入时机：

- 每次实际执行的 paadm 调用（包括 `user.resolve`、启动自检的 `version`/`init`）；
- 超时 / 找不到二进制 / 非法 JSON 等异常也会写一行（`success=0`，带 `error_code`）；
- **参数解析失败不会调用 paadm，也不会写日志**；
- 写日志失败只告警，不影响业务回复。

面板可直接查询，例如：

```sql
SELECT ts, source, action, success, exit_code, duration_ms, error_code
FROM interactions ORDER BY id DESC LIMIT 50;

SELECT action, COUNT(*) AS n, SUM(success=0) AS failures
FROM interactions GROUP BY action ORDER BY n DESC;
```

宿主机查看：

```bash
sqlite3 /opt/astrbot-new/data/paa/interactions.db \
  "SELECT ts, source, action, success, error_code FROM interactions ORDER BY id DESC LIMIT 20;"
```

---

## 7. 错误码

客户端把 paadm 失败稳定映射为：

| code | 含义 |
|---|---|
| `PAADM_NOT_FOUND` | 找不到 paadm 可执行文件 / 不可执行 |
| `TIMEOUT` | 超过 `timeout_seconds` |
| `INVALID_OUTPUT` | stdout 不是约定 JSON，或 `success` 与退出码矛盾 |
| `SPAWN_ERROR` | 进程启动失败（权限等） |
| `PAADM_ERROR` | 其它执行失败 |
| 业务码 | `TASK_NOT_FOUND`、`SCHEDULE_NOT_FOUND`、`NOTE_NOT_FOUND`、`INVALID_ARGUMENT`、`DATABASE_BUSY` 等按 paadm 的原样透传 |

---

## 8. 启动自检与降级

`initialize()` 和 `on_astrbot_loaded()` 会各触发一次自检（内部幂等，只跑一次）：

1. `paadm --json version`；
2. `paadm --json init`（显式建库/迁移，幂等）。

成功与失败都写 `source='startup'` 的交互日志并打 AstrBot 日志；**任何失败只告警，
不抛出异常**，插件仍可加载，业务命令会返回可读错误。

---

## 9. 测试

本机（`python3.10+`，无需 AstrBot / 服务器）：

```bash
cd plugin/astrbot_plugin_paadm
python3 -m py_compile main.py *.py
python3 -m unittest discover -s tests -v
```

测试用 `sys.modules` 注入最小 astrbot stub、用 `#!/usr/bin/env python3` 的假 paadm
脚本验证成功/失败/非法 JSON/超时/不存在/用户缓存等路径；另有一组可选的真实
paadm 端到端测试（找到 `../../paadm/target/debug/paadm` 或 `PAADM_TEST_BIN` 时运行，
否则自动 skip），用来验证 argv 与真实 clap 参数完全一致。

---

## 10. 排障

**1）确认容器内 paadm 可执行：**

```bash
docker exec -it astrbot-new sh -lc \
  'ls -l /AstrBot/data/paa/bin/paadm && /AstrBot/data/paa/bin/paadm --json version'
# 期望：{"success":true,"data":{"name":"paadm","version":"..."},"error":null}
```

**2）权限 / 架构：**

```bash
docker exec astrbot-new sh -lc 'chmod +x /AstrBot/data/paa/bin/paadm'
docker exec astrbot-new sh -lc 'file /AstrBot/data/paa/bin/paadm'   # 需匹配容器架构
```

**3）数据库可写：**

```bash
docker exec -it astrbot-new sh -lc \
  'PAADM_DB_PATH=/AstrBot/data/paa/paa.db /AstrBot/data/paa/bin/paadm --json init'
```

**4）看插件日志：**

```bash
docker logs astrbot-new 2>&1 | grep -i paadm | tail -50
```

重点看：`[paadm] v1.0.0 已加载`、`启动自检通过`、`启动自检失败`、`写 interactions 日志失败`。

**5）命令报 `PAADM_NOT_FOUND`：**
插件配置的 `paadm_bin` 与容器内实际路径不一致，或文件没有执行权限。
**命令报 `TIMEOUT`：** 磁盘/数据库锁问题，检查 `paa.db-wal` 是否异常，或调大
`timeout_seconds`。
**命令报 `INVALID_OUTPUT`：** 用第 1 步手动执行同一条命令，确认 paadm 版本与
输出 JSON 契约；stderr 已写入 `interactions.db` 可查。
**面板没有数据：** 确认 `interactions_db` 指向的目录可写，且插件加载日志里没有
`初始化交互日志库失败`。

**6）重载插件（改配置后）：** 在 AstrBot WebUI 插件页点击重载，或重启容器。

**7）`/paa task add ...` 只收到 `task`、报「缺少子命令」：**
AstrBot 4.29.4 的 `CommandFilter.init_handler_md` 对**带默认值**的参数只记录默认值、
丢掉 `GreedyStr` 标记。本插件已把签名固定为无默认值的
`async def paa_cmd(self, event, args: GreedyStr)`，并由
`tests/test_contract.py::test_paa_command_args_is_bare_greedystr` 回归保护；
二次开发时不要给它加默认值。

---

## 11. 设计说明

- `main.py`：**只放 handler**（命令 / LLM 工具 / 生命周期）。AstrBot 4.29.4 按
  `handler.__module__ == data.plugins.<目录名>.main` 绑定插件实例，放子模块会失效。
- `command_router.py`：纯逻辑（不 import astrbot、不 IO），负责参数解析、金额换算、
  ISO 校验、argv 构造与中文回复格式化。
- `paadm_client.py`：subprocess 调用、JSON 契约解析、稳定错误码、用户 resolve 缓存
  （`threading.Lock`），**永不把业务错误抛给调用方**。
- `interaction_store.py`：stdlib sqlite3 日志库，表结构固定供面板依赖。
- 阻塞的 `subprocess.run` 统一用 `asyncio.to_thread` 包进线程池，避免卡住 AstrBot
  事件循环（最长受 `timeout_seconds` 约束）。

---

## 附：本机面板隧道守护脚本（scripts/paa-tunnel.sh）

`scripts/paa-tunnel.sh` 是在**本机**（用户电脑）运行的 SSH 隧道看门狗：
电脑重启/休眠唤醒/锁屏断网后自动重连 `127.0.0.1:8765`（PAA 数据面板）与
`127.0.0.1:16185`（AstrBot WebUI），并支持 `systemd --user` 开机自启：

```bash
cd scripts
./paa-tunnel.sh install   # 安装 systemd 用户服务 + 立即启动
./paa-tunnel.sh status    # 状态 / 健康检查 / 最近日志
./paa-tunnel.sh logs -f   # 跟踪日志
```

> 实际部署位置为工作区 `plugin/scripts/`（systemd unit 的 ExecStart 指向它），
> 本目录内的副本随仓库发布；详见 [scripts/README.md](scripts/README.md)。
