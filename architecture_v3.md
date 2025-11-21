# P4 Integration - 安全分布式 Agent 架构设计文档 (v3.0)

本文档定义了系统的最终技术规格，集成了 TCP 主从架构、严格业务状态机以及企业级安全与可观测性增强。

---

## 1. 系统概述 (System Overview)

### 1.1 核心目标
构建一个安全、可靠、可观测的分布式任务执行系统，替代不稳定的 SSH 轮询模式。
*   **反向连接**: Agent (Remote) -> Master (Web)，穿透防火墙。
*   **零信任安全**: 全链路加密、身份认证、指令签名。
*   **状态一致性**: 掉电/断网后自动恢复，任务状态持久化。

### 1.2 架构图解
```
[ Master (Web Server) ]  <==== mTLS Encrypted TCP ====>  [ Agent (Remote Linux) ]
       |                                                         |
  [ State Machine ]                                         [ Executor ]
       |                                                         |
  [ Persistence DB ]                                        [ P4 Client ]
```

---

## 2. 组件详细设计

### A. Master (主控端)
*   **AgentServer (Secure)**:
    *   监听端口: `9090` (TLS Enabled)。
    *   功能: 维护 Agent 长连接池，心跳检测，命令分发。
*   **JobStateMachine (持久化)**:
    *   功能: 管理任务全生命周期，每次状态变更立即写入磁盘 (SQLite/JSON)。
    *   恢复: 启动时加载未完成任务，重新连接 Agent。
*   **Bootstrapper (审计)**:
    *   功能: SSH 注入 Agent 代码。
    *   安全: 记录详细审计日志 (Who, When, Target IP)。

### B. Agent (执行端)
*   **Security Layer**:
    *   启动时生成临时 RSA 密钥对。
    *   验证 Master 指令签名，拒绝未授权命令。
*   **Execution Engine**:
    *   单任务串行执行 (One Job at a Time)。
    *   **沙箱化**: 每个任务在独立临时目录运行，结束后强制清理。
*   **Watchdog**:
    *   断网保护: 连接断开 > 5分钟，自动杀掉所有子进程并自毁。

---

## 3. 通信协议 (Secure CmdProtocol)

基于 TLS 的 JSON Lines 协议。所有包必须包含 `sig` (签名)。

### 3.1 Master -> Agent (指令)
| 类型 | 关键字段 | 安全机制 | 描述 |
| :--- | :--- | :--- | :--- |
| `EXEC_CMD` | `cmd_id`, `command`, `env`, `timeout` | **Signed** | 执行命令 (带超时) |
| `KILL_CMD` | `cmd_id`, `signal` | **Signed** | 强制终止 |
| `HANDSHAKE`| `server_cert`, `challenge` | mTLS | 初始握手 |

### 3.2 Agent -> Master (事件)
| 类型 | 关键字段 | 描述 |
| :--- | :--- | :--- |
| `REGISTER` | `agent_id`, `pub_key` | 注册并交换公钥 |
| `LOG` | `cmd_id`, `stream`, `data` | 实时日志流 |
| `CMD_RESULT`| `cmd_id`, `exit_code`, `metrics` | **结构化**结果 (不仅是文本) |
| `HEARTBEAT` | `status`, `current_job_id` | 状态同步 |

---

## 4. 严格业务状态机 (Strict State Machine)

Master 严格按照此表流转状态。每一步都先持久化，再发命令。

| 阶段 (Stage) | 执行动作 (Action) | 成功流转 (Exit=0) | 失败流转 (Exit!=0) | 备注 |
| :--- | :--- | :--- | :--- | :--- |
| **SYNC** | `init.sh && bootenv && p4w sync` | **INTEGRATE** | **ERROR** | 环境准备 |
| **INTEGRATE** | `p4 integrate -b spec ...` | **RESOLVE_PASS_1** | **ERROR** | 集成 |
| **RESOLVE_PASS_1**| `p4 resolve -am` | **RESOLVE_PASS_2** | **ERROR** | 自动解决 (1/2) |
| **RESOLVE_PASS_2**| `p4 resolve -am` | **RESOLVE_CHECK** | **ERROR** | 自动解决 (2/2) |
| **RESOLVE_CHECK** | `p4 resolve -n` | **PRE_SUBMIT** (无冲突)<br>**NEEDS_RESOLVE** (有) | **ERROR** | Agent 解析结构化结果 |
| **NEEDS_RESOLVE** | (等待人工干预) | **RESOLVE_CHECK** | - | 暂停 |
| **PRE_SUBMIT** | 1. Master 查 Blocklist<br>2. Agent 跑 Test Hook | **SHELVE** | **BLOCKED** | 提交前检查 |
| **SHELVE** | `p4 shelve -f -c CL` | **P4PUSH** | **NC_FIX** (若含name_check)<br>**ERROR** (其他) | Shelve |
| **NC_FIX** | `fix_script.sh` | **SHELVE** | **ERROR** | 自动修复 |
| **P4PUSH** | `p4push -c CL` | **DONE** | **ERROR** | 推送 |

---

## 5. 安全与增强特性 (Security & Robustness)

### 5.1 零信任安全 (Zero Trust)
1.  **mTLS**: Master 和 Agent 双向验证证书，防止伪装。
2.  **指令签名**: Master 用私钥签名 `hash(cmd_id + command)`，Agent 用公钥验签。防止中间人篡改指令。
3.  **一次性 Token**: Bootstrap 时生成短效 Token，Agent 注册时必须携带，否则拒绝连接。

### 5.2 状态一致性 (Consistency)
*   **幂等性 (Idempotency)**: 每个 `cmd_id` 全局唯一。Agent 记录已执行的 `cmd_id`，收到重复指令直接返回缓存结果，防止网络重发导致重复执行。
*   **断点续传**: Agent 重连时发送 `SYNC_STATE`，告知 Master 当前正在跑什么。Master 据此恢复上下文，而不是盲目重启任务。

### 5.3 结构化日志与监控 (Observability)
*   **Agent 解析器**: Agent 不再只发回一堆文本，而是内置简单的 Parser。
    *   例: `p4 resolve -n` 的输出在 Agent 端被解析为 `{"conflict_count": 5, "files": [...]}` 发给 Master。Master 逻辑不再依赖脆弱的字符串匹配。
*   **指标监控**: Master 暴露 `/metrics` 接口 (Prometheus 格式)，监控：
    *   `active_agents`: 在线节点数。
    *   `job_duration_seconds`: 任务耗时分布。
    *   `p4_error_rate`: P4 命令失败率。

### 5.4 资源隔离与清理
*   **工作目录隔离**: Agent 为每个 Job 创建 `/tmp/p4_integ_<job_id>` 目录。
*   **自动清理**: 无论成功失败，Job 结束时 Agent 自动 `rm -rf` 该目录，并 `kill` 遗留进程。

---

**实施计划**:
1.  **Phase 1**: 完成 TLS 通信与 Agent 原型。
2.  **Phase 2**: 实现 Master 状态机与持久化。
3.  **Phase 3**: 集成安全特性 (签名/Token)。
4.  **Phase 4**: 结构化解析与 UI 对接。

