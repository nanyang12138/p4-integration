# P4 Integration - 分布式 Agent 架构设计文档 (Final v2.0)

## 1. 系统概述
本系统采用 **TCP Socket 主从架构** 替代原有的 SSH 轮询模式。
*   **目标**: 解决进程状态监控不准确、日志显示混乱、长连接不稳定等问题。
*   **核心机制**: Agent (远程) 主动连接 Master (本地)，建立持久化全双工通道，通过事件驱动状态流转。

## 2. 组件架构

### A. Master (主控端 - Web Server)
*   **角色**: 大脑。负责决策、调度和状态管理。
*   **位置**: 您的本地机器或部署 Web 服务的机器。
*   **核心模块**:
    *   **AgentServer**: TCP 监听器 (端口 9090)，管理 Agent 连接池。
    *   **JobStateMachine**: 维护业务逻辑流转表 (见第 4 节)。
    *   **Bootstrapper**: 使用 SSH 将 Agent 代码注入远程内存运行 ("特洛伊木马"式部署)。

### B. Agent (执行端 - Remote Linux)
*   **角色**: 工人。负责执行命令和汇报原始数据。
*   **位置**: 远程 Linux 开发机。
*   **生命周期**: 随任务启动，任务结束或空闲超时后自动销毁 (无残留)。
*   **核心模块**:
    *   **SocketClient**: 主动连接 Master，断线重连。
    *   **CommandExecutor**: 线程池，使用 `subprocess.Popen` 执行命令。
    *   **LogStreamer**: 实时读取 stdout/stderr 并打包发送。

## 3. 通信协议 (CmdProtocol)
基于 TCP 的 JSON Lines 协议 (每行一个 JSON 对象)。

### 3.1 Master -> Agent (指令)
| 类型 (`type`) | 参数 | 描述 |
| :--- | :--- | :--- |
| `EXEC_CMD` | `cmd_id`, `command`, `cwd`, `env` | 在指定目录和环境下执行 Shell 命令 |
| `KILL_CMD` | `cmd_id`, `signal` | 杀掉正在运行的子进程 |
| `HANDSHAKE_ACK`| `config` | 确认连接建立 |

### 3.2 Agent -> Master (事件)
| 类型 (`type`) | 参数 | 描述 |
| :--- | :--- | :--- |
| `REGISTER` | `hostname`, `ip` | Agent 启动后发送的首个包 |
| `LOG` | `cmd_id`, `stream` (stdout/stderr), `data` | **实时**日志数据块 |
| `CMD_DONE` | `cmd_id`, `exit_code` | 命令执行结束 (0=成功) |
| `HEARTBEAT` | `timestamp`, `load` | 每 5s 发送，保活 |

## 4. 严格业务流程 (State Machine)

Master 收到 `CMD_DONE` 事件后，根据当前 Stage 和 Exit Code 查表决定下一步。

| 阶段 (Stage) | Agent 执行动作 (Shell Command) | 判定逻辑 (Master 侧) | 下一阶段 (成功) | 下一阶段 (失败) |
| :--- | :--- | :--- | :--- | :--- |
| **SYNC** | `source init.sh && bootenv && p4w sync` | `exit_code == 0` | **INTEGRATE** | **ERROR** |
| **INTEGRATE** | `p4 integrate -b spec ...` | `exit_code == 0` | **RESOLVE_PASS_1** | **ERROR** |
| **RESOLVE_PASS_1** | `p4 resolve -am` (第1轮) | (无条件继续) | **RESOLVE_PASS_2** | **ERROR** |
| **RESOLVE_PASS_2** | `p4 resolve -am` (第2轮) | (无条件继续) | **RESOLVE_CHECK** | **ERROR** |
| **RESOLVE_CHECK** | `p4 resolve -n` | 解析日志内容:<br>1. 含 "merging" -> 有冲突<br>2. 含 "No file(s) to resolve" -> 无冲突 | **PRE_SUBMIT** (无冲突)<br>**NEEDS_RESOLVE** (有冲突) | **ERROR** |
| **NEEDS_RESOLVE** | (等待用户手动操作) | 用户点击 "Continue" | **RESOLVE_CHECK** | - |
| **PRE_SUBMIT** | (如有) `make check` 等 | 1. 本地查 Blocklist<br>2. 远程 Hook 返回 0 | **SHELVE** | **BLOCKED** |
| **SHELVE** | `p4 shelve -f -c CL` | 检查 stderr:<br>1. 含 "name_check" -> **NC_FIX**<br>2. 否则 -> **P4PUSH** | **P4PUSH** | **ERROR** |
| **NC_FIX** | `bash fix_script.sh` (revert+reshelve) | `exit_code == 0` | **SHELVE** (重试) | **ERROR** |
| **P4PUSH** | `p4push -c CL` | `exit_code == 0` | **DONE** | **ERROR** |

## 5. 关键技术实现细节

### 5.1 自动部署 (Bootstrap)
Master 使用 `paramiko` 执行以下 Shell 命令来启动 Agent：
```bash
nohup python3 -c "import base64,sys; exec(base64.b64decode('...AGENT_CODE_BASE64...'))" > /dev/null 2>&1 &
```
Agent 启动后立即连接 Master IP。

### 5.2 日志处理 (Log UI)
*   **后端**: Master 将收到的 `LOG` 包存入文件（持久化），同时写入内存队列。
*   **前端**: 网页通过 WebSocket 或 SSE (Server-Sent Events) 订阅队列。
*   **展示**: 使用 xterm.js 或类似组件，将 stdout/stderr 分色显示，彻底解决格式混乱问题。

### 5.3 异常恢复
*   **断线重连**: Agent 内部实现指数退避重连 (Exponential Backoff)。
*   **Agent 僵死**: Master 标记任务为 `OFFLINE`，允许用户在 UI 点击 "Reconnect" 再次触发 Bootstrap。

