# P4 集成系统架构设计文档 v3.0 (安全增强版)

## 一、系统概述

### 1.1 核心改进
在 v2.0 的基础上，v3.0 版本着重解决以下关键问题：
- **安全性**: 零信任架构，TLS加密、双向认证、命令签名
- **状态一致性**: 内存状态机、断线重连、幂等执行
- **可观测性**: 结构化日志、指标监控、审计追踪
- **资源隔离**: 命令隔离执行、超时控制、自动清理

### 1.2 架构图
```
┌─────────────────┐         TLS/mTLS          ┌─────────────────┐
│   Web UI        │◄──────────────────────────┤   AgentServer   │
│  (Flask)        │         WebSocket          │   (Port 9090)   │
└────────┬────────┘                            └────────┬────────┘
         │                                               │
         │ REST API                                      │ Protocol
         │                                               │
┌────────▼────────┐                            ┌────────▼────────┐
│   Master        │                            │   Agent         │
│  - Scheduler    │         Bootstrap          │  - Executor     │
│  - JobState     ├───────────────────────────►│  - Parser       │
│  - JobState     │            SSH              │  - Security     │
└─────────────────┘                            └─────────────────┘
```

## 二、安全架构 (新增)

### 2.1 传输层安全 - mTLS
```python
# Master端 TLS 配置
ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
ssl_context.load_cert_chain(
    certfile="certs/master.crt",
    keyfile="certs/master.key"
)
ssl_context.load_verify_locations("certs/ca.crt")
ssl_context.verify_mode = ssl.CERT_REQUIRED  # 要求客户端证书
```

```python
# Agent端 TLS 配置
ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
ssl_context.load_cert_chain(
    certfile="certs/agent-{hostname}.crt",
    keyfile="certs/agent-{hostname}.key"
)
ssl_context.check_hostname = True
ssl_context.verify_mode = ssl.CERT_REQUIRED
```

### 2.2 身份认证与授权

#### 2.2.1 Agent 白名单
```yaml
# config/agent_whitelist.yaml
allowed_agents:
  - hostname: "srdcws123"
    cert_cn: "agent.srdcws123.amd.com"
    ip_range: "10.194.0.0/24"
  - hostname: "srdcws456"
    cert_cn: "agent.srdcws456.amd.com"
    ip_range: "10.195.0.0/24"
```

#### 2.2.2 短期令牌机制
```python
# Master生成一次性令牌
def generate_command_token(cmd_id: str, agent_id: str) -> str:
    payload = {
        "cmd_id": cmd_id,
        "agent_id": agent_id,
        "exp": time.time() + 3600,  # 1小时有效
        "nonce": secrets.token_hex(16)
    }
    return jwt.encode(payload, MASTER_SECRET, algorithm="HS256")
```

#### 2.2.3 命令签名
```python
# Master端签名
def sign_command(cmd: dict) -> str:
    message = json.dumps(cmd, sort_keys=True)
    signature = private_key.sign(
        message.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH
        ),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode()

# Agent端验证
def verify_command(cmd: dict, signature: str) -> bool:
    message = json.dumps(cmd, sort_keys=True)
    try:
        public_key.verify(
            base64.b64decode(signature),
            message.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
        return True
    except Exception:
        return False
```

### 2.3 Bootstrap 安全审计

#### 2.3.1 审计日志格式
```json
{
    "timestamp": "2025-01-02T10:30:45Z",
    "event_type": "BOOTSTRAP_ATTEMPT",
    "user": "nanyang2",
    "target_host": "srdcws123",
    "target_ip": "10.194.1.23",
    "ssh_command_hash": "sha256:abc123...",
    "result": "SUCCESS",
    "agent_id": "agent-srdcws123-12345",
    "duration_ms": 2341
}
```

#### 2.3.2 最小权限执行
```bash
# Agent 运行在受限用户下
useradd -m -s /bin/bash -G p4users p4agent
# 只能访问工作目录
chmod 700 /home/p4agent
# 无 sudo 权限
```

## 三、状态一致性与恢复机制 (增强)

### 3.1 内存状态管理

#### 3.1.1 状态存储结构
```python
# 纯内存状态管理
class InMemoryJobState:
    def __init__(self):
        self.jobs = {}  # job_id -> state info
        self.command_log = {}  # cmd_id -> command result
        self.lock = asyncio.Lock()  # 并发控制
    
    async def update_state(self, job_id: str, new_stage: str, metadata: dict = None):
        async with self.lock:
            if job_id not in self.jobs:
                self.jobs[job_id] = {
                    "created_at": datetime.now(),
                    "history": []
                }
            
            self.jobs[job_id].update({
                "current_stage": new_stage,
                "updated_at": datetime.now(),
                "metadata": metadata or {},
                "history": self.jobs[job_id]["history"] + [(new_stage, datetime.now())]
            })
    
    def get_state(self, job_id: str) -> dict:
        return self.jobs.get(job_id, {})
    
    def log_command(self, cmd_id: str, job_id: str, command: str, result: dict):
        self.command_log[cmd_id] = {
            "job_id": job_id,
            "command": command,
            "sent_at": datetime.now(),
            "result": result,
            "exit_code": result.get("exit_code"),
            "output": result.get("stdout", "") + result.get("stderr", "")
        }
```

#### 3.1.2 状态转换原子性
```python
async def transition_stage(self, job_id: str, from_stage: str, to_stage: str, data: dict):
    async with self.state.lock:
        current = self.state.get_state(job_id)
        if current.get("current_stage") != from_stage:
            raise StateConflictError(
                f"Job {job_id} not in expected stage {from_stage}, "
                f"currently in {current.get('current_stage')}"
            )
        
        await self.state.update_state(job_id, to_stage, data)
```

### 3.2 断线重连协议

#### 3.2.1 Agent 重连流程
```python
# Agent 连接/重连时的握手协议
async def agent_handshake(self):
    # 1. 发送身份信息
    await self.send({
        "type": "AGENT_HELLO",
        "agent_id": self.agent_id,
        "version": "3.0.0",
        "capabilities": ["p4", "git", "ssh"],
        "last_cmd_id": self.last_executed_cmd_id  # 用于同步
    })
    
    # 2. 接收 Master 响应
    response = await self.recv()
    if response["type"] == "SYNC_REQUIRED":
        # Master 告知有未完成的命令
        pending_cmds = response["pending_commands"]
        for cmd in pending_cmds:
            if cmd["cmd_id"] <= self.last_executed_cmd_id:
                # 幂等性检查：已执行过的命令
                await self.send({
                    "type": "CMD_ALREADY_DONE",
                    "cmd_id": cmd["cmd_id"],
                    "cached_result": self.get_cached_result(cmd["cmd_id"])
                })
            else:
                # 执行新命令
                await self.execute_command(cmd)
```

#### 3.2.2 Master 端重连处理
```python
class MasterReconnectHandler:
    def handle_agent_hello(self, agent_id: str, last_cmd_id: str):
        # 1. 验证 Agent 身份
        if not self.verify_agent(agent_id):
            return {"type": "AUTH_FAILED"}
        
        # 2. 查询该 Agent 的未完成任务
        pending_jobs = [
            job for job_id, job in self.state.jobs.items()
            if job.get("agent_id") == agent_id and 
               job.get("current_stage") != "COMPLETED"
        ]
        
        # 3. 获取需要重发的命令
        pending_commands = []
        for job in pending_jobs:
            # 查找该任务最后执行的命令
            job_cmds = [
                (cmd_id, cmd) for cmd_id, cmd in self.state.command_log.items()
                if cmd["job_id"] == job["job_id"]
            ]
            if job_cmds:
                latest_cmd = max(job_cmds, key=lambda x: x[0])  # cmd_id 是递增的
                if latest_cmd[0] > last_cmd_id:
                    pending_commands.append(self.reconstruct_command(latest_cmd[1]))
        
        return {
            "type": "SYNC_REQUIRED" if pending_commands else "SYNC_OK",
            "pending_commands": pending_commands
        }
```

### 3.3 幂等性保证

```python
# 每个命令都有唯一ID，防止重复执行
class IdempotentExecutor:
    def __init__(self):
        self.executed_commands = {}  # cmd_id -> result
    
    async def execute(self, cmd: dict):
        cmd_id = cmd["cmd_id"]
        
        # 检查是否已执行
        if cmd_id in self.executed_commands:
            return self.executed_commands[cmd_id]
        
        # 记录命令意图
        self.command_intents[cmd_id] = cmd
        
        try:
            result = await self._do_execute(cmd)
            self.executed_commands[cmd_id] = result
            self.state.log_command(cmd_id, cmd["job_id"], cmd["command"], result)
            return result
        except Exception as e:
            self.command_errors[cmd_id] = str(e)
            raise
```

## 四、连接管理与扩展性 (新增)

### 4.1 连接池管理
```python
class AgentConnectionPool:
    def __init__(self, max_connections: int = 100):
        self.max_connections = max_connections
        self.connections = {}  # agent_id -> connection
        self.connection_stats = {}  # 连接统计
        
    async def add_connection(self, agent_id: str, conn):
        if len(self.connections) >= self.max_connections:
            # 实施背压策略
            oldest = min(self.connections.items(), 
                        key=lambda x: x[1].last_active)
            await self.close_connection(oldest[0])
        
        self.connections[agent_id] = conn
        self.connection_stats[agent_id] = {
            "connected_at": datetime.now(),
            "last_active": datetime.now(),
            "commands_sent": 0,
            "bytes_sent": 0
        }
```

### 4.2 健康检查
```python
class HealthChecker:
    async def check_agent_health(self, agent_id: str):
        try:
            # 1. 发送 PING
            await self.send_to_agent(agent_id, {"type": "PING"})
            
            # 2. 等待 PONG (超时 5秒)
            response = await asyncio.wait_for(
                self.recv_from_agent(agent_id), 
                timeout=5.0
            )
            
            if response["type"] == "PONG":
                # 3. 更新健康状态
                self.update_health_status(agent_id, "HEALTHY")
                return True
        except asyncio.TimeoutError:
            self.update_health_status(agent_id, "TIMEOUT")
            return False
```

### 4.3 负载均衡策略
```python
class LoadBalancer:
    def select_agent(self, job_requirements: dict) -> str:
        available_agents = self.get_healthy_agents()
        
        # 1. 过滤符合要求的 Agent
        suitable_agents = [
            agent for agent in available_agents
            if self.meets_requirements(agent, job_requirements)
        ]
        
        # 2. 选择负载最低的
        if suitable_agents:
            return min(suitable_agents, 
                      key=lambda a: self.get_agent_load(a))
        
        return None
```

## 五、命令执行隔离 (新增)

### 5.1 沙箱执行环境
```python
class SandboxedExecutor:
    def __init__(self, workspace_root: str):
        self.workspace_root = workspace_root
        
    async def execute_in_sandbox(self, cmd: dict):
        # 1. 创建临时工作目录
        job_id = cmd["job_id"]
        sandbox_dir = f"{self.workspace_root}/.sandboxes/{job_id}"
        os.makedirs(sandbox_dir, exist_ok=True)
        
        # 2. 设置资源限制
        env = os.environ.copy()
        env.update({
            "TMPDIR": sandbox_dir,
            "HOME": sandbox_dir,
            "P4CLIENT": f"{job_id}_temp"
        })
        
        # 3. 使用 cgroups 限制资源 (Linux)
        resource_limits = {
            "cpu": "50%",      # CPU 使用率上限
            "memory": "2G",    # 内存上限
            "io": "10M"        # IO 带宽上限
        }
        
        try:
            # 4. 执行命令
            process = await asyncio.create_subprocess_exec(
                *cmd["args"],
                env=env,
                cwd=sandbox_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=lambda: self._set_resource_limits(resource_limits)
            )
            
            stdout, stderr = await process.communicate()
            return {
                "exit_code": process.returncode,
                "stdout": stdout.decode(),
                "stderr": stderr.decode()
            }
        finally:
            # 5. 清理沙箱
            await self._cleanup_sandbox(sandbox_dir)
```

### 5.2 容器化执行 (可选)
```python
class ContainerizedExecutor:
    async def execute_in_container(self, cmd: dict):
        container_config = {
            "image": "p4-agent:latest",
            "volumes": {
                self.workspace_root: {
                    "bind": "/workspace",
                    "mode": "rw"
                }
            },
            "environment": {
                "P4PORT": self.p4_config["port"],
                "P4USER": self.p4_config["user"]
            },
            "mem_limit": "2g",
            "cpu_quota": 50000,  # 50% CPU
            "network_mode": "none"  # 禁用网络
        }
        
        # 使用 Docker SDK
        container = self.docker_client.containers.run(
            command=cmd["args"],
            **container_config,
            detach=True
        )
        
        # 等待执行完成
        result = container.wait()
        logs = container.logs(stdout=True, stderr=True)
        container.remove()
        
        return {
            "exit_code": result["StatusCode"],
            "output": logs.decode()
        }
```

## 六、可观测性与监控 (增强)

### 6.1 结构化日志
```python
# 日志格式定义
class StructuredLogger:
    def log_command(self, level: str, event: str, **kwargs):
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": level,
            "event": event,
            "job_id": kwargs.get("job_id"),
            "cmd_id": kwargs.get("cmd_id"),
            "agent_id": kwargs.get("agent_id"),
            "stage": kwargs.get("stage"),
            "duration_ms": kwargs.get("duration_ms"),
            "error": kwargs.get("error"),
            "metadata": kwargs.get("metadata", {})
        }
        
        # 写入 JSON Lines 格式
        with open("logs/commands.jsonl", "a") as f:
            f.write(json.dumps(log_entry) + "\n")
```

### 6.2 指标收集
```python
# Prometheus 兼容的指标
class MetricsCollector:
    def __init__(self):
        self.metrics = {
            # Counter 类型
            "jobs_total": Counter("p4_jobs_total", "Total number of jobs"),
            "jobs_success": Counter("p4_jobs_success", "Successful jobs"),
            "jobs_failed": Counter("p4_jobs_failed", "Failed jobs"),
            
            # Gauge 类型
            "agents_connected": Gauge("p4_agents_connected", "Connected agents"),
            "jobs_in_progress": Gauge("p4_jobs_in_progress", "Jobs in progress"),
            
            # Histogram 类型
            "job_duration": Histogram("p4_job_duration_seconds", 
                                    "Job duration", 
                                    buckets=[60, 300, 600, 1800, 3600])
        }
    
    def expose_metrics(self):
        # 暴露 /metrics 端点
        return prometheus_client.generate_latest()
```

### 6.3 告警规则
```yaml
# prometheus/alerts.yml
groups:
  - name: p4_integration
    rules:
      - alert: HighJobFailureRate
        expr: rate(p4_jobs_failed[5m]) > 0.1
        for: 10m
        annotations:
          summary: "High job failure rate"
          description: "Job failure rate is {{ $value }} per second"
      
      - alert: AgentOffline
        expr: p4_agents_connected < 1
        for: 5m
        annotations:
          summary: "No agents connected"
      
      - alert: LongRunningJob
        expr: p4_job_duration_seconds > 3600
        annotations:
          summary: "Job running for over 1 hour"
```

### 6.4 日志轮转与归档
```python
class LogRotator:
    def __init__(self, log_dir: str, max_size_mb: int = 100, max_files: int = 10):
        self.log_dir = log_dir
        self.max_size_mb = max_size_mb
        self.max_files = max_files
        
    async def rotate_logs(self):
        for log_file in Path(self.log_dir).glob("*.jsonl"):
            if log_file.stat().st_size > self.max_size_mb * 1024 * 1024:
                # 压缩旧日志
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                archive_name = f"{log_file.stem}_{timestamp}.jsonl.gz"
                
                with open(log_file, 'rb') as f_in:
                    with gzip.open(f"{self.log_dir}/archive/{archive_name}", 'wb') as f_out:
                        f_out.writelines(f_in)
                
                # 清空当前日志
                log_file.write_text("")
                
                # 删除过期归档
                self._cleanup_old_archives()
```

## 七、异常恢复与降级 (新增)

### 7.1 自动重试机制
```python
class RetryStrategy:
    def __init__(self):
        self.retry_config = {
            "NETWORK_ERROR": {"max_attempts": 3, "backoff": "exponential"},
            "P4_LOCKED": {"max_attempts": 5, "backoff": "linear", "delay": 30},
            "TIMEOUT": {"max_attempts": 2, "backoff": "fixed", "delay": 60}
        }
    
    async def execute_with_retry(self, func, error_classifier):
        attempt = 0
        last_error = None
        
        while attempt < self.get_max_attempts(error_classifier(last_error)):
            try:
                return await func()
            except Exception as e:
                last_error = e
                error_type = error_classifier(e)
                
                if error_type not in self.retry_config:
                    raise  # 不可重试的错误
                
                attempt += 1
                delay = self.calculate_delay(error_type, attempt)
                await asyncio.sleep(delay)
        
        raise last_error
```

### 7.2 熔断器模式
```python
class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failures = 0
        self.last_failure_time = None
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
    
    async def call(self, func):
        if self.state == "OPEN":
            if time.time() - self.last_failure_time > self.timeout:
                self.state = "HALF_OPEN"
            else:
                raise CircuitOpenError("Circuit breaker is open")
        
        try:
            result = await func()
            if self.state == "HALF_OPEN":
                self.state = "CLOSED"
                self.failures = 0
            return result
        except Exception as e:
            self.failures += 1
            self.last_failure_time = time.time()
            
            if self.failures >= self.failure_threshold:
                self.state = "OPEN"
            
            raise
```

### 7.3 故障剧本
```python
# 预定义的故障恢复流程
class RecoveryPlaybook:
    def __init__(self):
        self.playbooks = {
            "MASTER_CRASH": [
                ("检查进程", self.check_master_process),
                ("尝试重启", self.restart_master),
                ("恢复状态", self.restore_from_checkpoint),
                ("通知管理员", self.notify_admin)
            ],
            "AGENT_HANG": [
                ("发送健康检查", self.health_check),
                ("强制断开", self.force_disconnect),
                ("SSH重启Agent", self.ssh_restart_agent),
                ("重新分配任务", self.reassign_jobs)
            ],
            "P4_SERVER_DOWN": [
                ("验证连接", self.verify_p4_connection),
                ("切换备用服务器", self.switch_to_backup_p4),
                ("暂停所有任务", self.pause_all_jobs),
                ("等待恢复", self.wait_for_recovery)
            ]
        }
```

## 八、并发与资源控制 (新增)

### 8.1 任务队列与优先级
```python
class PriorityJobQueue:
    def __init__(self):
        self.queues = {
            "HIGH": asyncio.PriorityQueue(),
            "NORMAL": asyncio.Queue(),
            "LOW": asyncio.Queue()
        }
        
    async def enqueue(self, job: dict, priority: str = "NORMAL"):
        job_wrapper = {
            "job": job,
            "enqueued_at": time.time(),
            "priority": priority
        }
        
        if priority == "HIGH":
            # 优先级队列，数字越小优先级越高
            priority_score = 0 if job.get("urgent") else 1
            await self.queues["HIGH"].put((priority_score, job_wrapper))
        else:
            await self.queues[priority].put(job_wrapper)
    
    async def dequeue(self) -> dict:
        # 优先处理高优先级任务
        for priority in ["HIGH", "NORMAL", "LOW"]:
            if not self.queues[priority].empty():
                if priority == "HIGH":
                    _, job_wrapper = await self.queues[priority].get()
                else:
                    job_wrapper = await self.queues[priority].get()
                return job_wrapper["job"]
        
        # 如果都为空，等待任意队列有任务
        return await self._wait_for_any_job()
```

### 8.2 资源配额管理
```python
class ResourceQuotaManager:
    def __init__(self):
        self.quotas = {
            "cpu_cores": 16,
            "memory_gb": 32,
            "concurrent_jobs": 10,
            "disk_io_mbps": 100
        }
        self.usage = defaultdict(float)
        
    async def acquire_resources(self, job_requirements: dict) -> bool:
        async with self.lock:
            # 检查资源是否足够
            for resource, required in job_requirements.items():
                if self.usage[resource] + required > self.quotas[resource]:
                    return False
            
            # 分配资源
            for resource, required in job_requirements.items():
                self.usage[resource] += required
            
            return True
    
    async def release_resources(self, job_requirements: dict):
        async with self.lock:
            for resource, required in job_requirements.items():
                self.usage[resource] -= required
```

### 8.3 超时与强制终止
```python
class TimeoutManager:
    def __init__(self):
        self.stage_timeouts = {
            "SYNC": 600,        # 10分钟
            "INTEGRATE": 1800,  # 30分钟
            "RESOLVE": 600,     # 10分钟
            "SHELVE": 300,      # 5分钟
            "P4PUSH": 1200     # 20分钟
        }
        
    async def execute_with_timeout(self, stage: str, coro):
        timeout = self.stage_timeouts.get(stage, 3600)
        
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            # 记录超时
            logger.error(f"Stage {stage} timeout after {timeout}s")
            
            # 尝试优雅终止
            await self.graceful_shutdown(coro)
            
            # 如果还未结束，强制终止
            await asyncio.sleep(30)
            await self.force_kill(coro)
            
            raise StageTimeoutError(f"{stage} exceeded {timeout}s limit")
```

## 九、结构化输出解析 (新增)

### 9.1 P4 输出解析器
```python
class P4OutputParser:
    def __init__(self):
        self.parsers = {
            "resolve": self.parse_resolve_output,
            "integrate": self.parse_integrate_output,
            "submit": self.parse_submit_output,
            "shelve": self.parse_shelve_output
        }
    
    def parse_resolve_output(self, output: str) -> dict:
        """解析 p4 resolve 输出为结构化数据"""
        result = {
            "conflicts": [],
            "auto_resolved": [],
            "no_files_to_resolve": False,
            "total_files": 0
        }
        
        # 检查无冲突情况
        if "No file(s) to resolve" in output or not output.strip():
            result["no_files_to_resolve"] = True
            return result
        
        # 解析每一行
        for line in output.splitlines():
            if " - merging " in line:
                # 自动解决的文件
                match = re.search(r'^(.*?) - merging', line)
                if match:
                    result["auto_resolved"].append(match.group(1))
            elif " - resolve skipped" in line or "non-text" in line:
                # 有冲突的文件
                match = re.search(r'^(.*?) - ', line)
                if match:
                    result["conflicts"].append({
                        "file": match.group(1),
                        "reason": "binary" if "non-text" in line else "conflict"
                    })
        
        result["total_files"] = len(result["auto_resolved"]) + len(result["conflicts"])
        return result
    
    def parse_integrate_output(self, output: str) -> dict:
        """解析 p4 integrate 输出"""
        result = {
            "integrated_files": [],
            "errors": [],
            "warnings": []
        }
        
        for line in output.splitlines():
            if "#" in line and " - " in line:
                # 成功集成的文件
                file_path = line.split(" - ")[0].split("#")[0]
                action = line.split(" - ")[1]
                result["integrated_files"].append({
                    "file": file_path,
                    "action": action
                })
            elif "error:" in line.lower():
                result["errors"].append(line)
            elif "warning:" in line.lower():
                result["warnings"].append(line)
        
        return result
```

### 9.2 状态判定逻辑
```python
class StageDecisionMaker:
    def decide_next_stage(self, current_stage: str, result: dict) -> str:
        """基于结构化结果决定下一阶段"""
        
        if current_stage == "RESOLVE_PASS_1":
            if result["no_files_to_resolve"]:
                return "PRE_SUBMIT"
            elif result["conflicts"]:
                return "RESOLVE_PASS_2"
            else:
                return "RESOLVE_CHECK"
        
        elif current_stage == "RESOLVE_PASS_2":
            if result["conflicts"]:
                # 仍有冲突，进入需要用户介入状态
                return "NEEDS_RESOLVE"
            else:
                return "RESOLVE_CHECK"
        
        elif current_stage == "RESOLVE_CHECK":
            # 基于 resolve -n 的结果
            if result["no_files_to_resolve"]:
                return "PRE_SUBMIT"
            else:
                return "FAILED"  # 还有未解决的冲突
        
        elif current_stage == "NEEDS_RESOLVE":
            # 这个状态下不会主动转换，需要等待自动检测或用户触发
            # 由 ResolveWatcher 负责监控和转换
            return "NEEDS_RESOLVE"  # 保持当前状态
        
        # ... 其他阶段的判定逻辑
```

### 9.3 冲突解决自动检测机制（NEEDS_RESOLVE 阶段）
```python
class ResolveWatcher:
    """
    后台监控服务，定期检测 NEEDS_RESOLVE 状态的任务
    自动执行 p4 resolve -n 来判断冲突是否已解决
    """
    def __init__(self, check_interval: int = 30):
        self.check_interval = check_interval  # 检查间隔（秒）
        self.watching_jobs = {}  # job_id -> watch_task
        
    async def start_watching(self, job_id: str):
        """开始监控一个进入 NEEDS_RESOLVE 状态的任务"""
        if job_id in self.watching_jobs:
            return  # 已经在监控中
        
        # 创建监控任务
        watch_task = asyncio.create_task(self._watch_job(job_id))
        self.watching_jobs[job_id] = watch_task
        
    async def _watch_job(self, job_id: str):
        """定期检查任务的冲突状态"""
        logger.info(f"Starting conflict resolution watch for job {job_id}")
        
        while True:
            try:
                # 检查任务当前状态
                current_state = self.state_manager.get_state(job_id)
                if current_state.get("current_stage") != "NEEDS_RESOLVE":
                    # 状态已变化（可能用户手动推进），停止监控
                    break
                
                # 自动执行 p4 resolve -n 检查
                logger.info(f"Auto-checking conflicts for job {job_id}")
                result = await self.execute_command({
                    "cmd": "p4 resolve -n",
                    "job_id": job_id,
                    "stage": "NEEDS_RESOLVE_CHECK"
                })
                
                # 解析结果
                parsed = self.parser.parse_resolve_output(result["stdout"])
                
                if parsed["no_files_to_resolve"]:
                    # 冲突已解决！自动转换到下一阶段
                    logger.info(f"Conflicts resolved for job {job_id}, transitioning to RESOLVE_CHECK")
                    
                    # 更新状态
                    await self.transition_stage(
                        job_id=job_id,
                        from_stage="NEEDS_RESOLVE",
                        to_stage="RESOLVE_CHECK",
                        metadata={
                            "resolved_at": datetime.now().isoformat(),
                            "auto_detected": True,
                            "resolve_check_output": result["stdout"]
                        }
                    )
                    
                    # 触发下一步执行（再次运行 resolve -n 作为最终确认）
                    await self.execute_next_stage(job_id, "RESOLVE_CHECK")
                    break
                else:
                    # 仍有冲突，记录检查结果
                    logger.debug(
                        f"Job {job_id} still has {len(parsed['conflicts'])} conflicts, "
                        f"will check again in {self.check_interval}s"
                    )
                
            except Exception as e:
                logger.error(f"Error in resolve watcher for job {job_id}: {e}")
            
            # 等待下一次检查
            await asyncio.sleep(self.check_interval)
        
        # 清理监控任务
        del self.watching_jobs[job_id]
        logger.info(f"Stopped conflict resolution watch for job {job_id}")
    
    async def stop_watching(self, job_id: str):
        """停止监控指定任务"""
        if job_id in self.watching_jobs:
            self.watching_jobs[job_id].cancel()
            del self.watching_jobs[job_id]
    
    async def handle_user_continue(self, job_id: str):
        """处理用户点击 Continue 按钮"""
        logger.info(f"User clicked continue for job {job_id}")
        
        # 立即执行一次检查，而不是等待下一个周期
        current_state = self.state_manager.get_state(job_id)
        if current_state.get("current_stage") == "NEEDS_RESOLVE":
            # 执行 resolve -n
            result = await self.execute_command({
                "cmd": "p4 resolve -n",
                "job_id": job_id,
                "stage": "NEEDS_RESOLVE_CHECK"
            })
            
            parsed = self.parser.parse_resolve_output(result["stdout"])
            
            if parsed["no_files_to_resolve"]:
                # 冲突已解决
                await self.transition_stage(
                    job_id=job_id,
                    from_stage="NEEDS_RESOLVE",
                    to_stage="RESOLVE_CHECK",
                    metadata={
                        "resolved_at": datetime.now().isoformat(),
                        "user_triggered": True,
                        "resolve_check_output": result["stdout"]
                    }
                )
                await self.execute_next_stage(job_id, "RESOLVE_CHECK")
            else:
                # 仍有冲突，通知用户
                await self.notify_user(
                    job_id,
                    f"Still have {len(parsed['conflicts'])} conflicts to resolve",
                    parsed['conflicts']
                )
```

### 9.4 状态机集成
```python
class JobStateMachine:
    def __init__(self):
        self.resolve_watcher = ResolveWatcher(check_interval=30)
        
    async def handle_stage_transition(self, job_id: str, current_stage: str, result: dict):
        """处理阶段转换，包括自动启动监控"""
        
        next_stage = self.decision_maker.decide_next_stage(current_stage, result)
        
        if next_stage == "NEEDS_RESOLVE":
            # 进入需要人工介入的状态，启动自动监控
            await self.resolve_watcher.start_watching(job_id)
            
            # 通知用户有冲突需要解决
            conflicts = result.get("conflicts", [])
            await self.notify_user(
                job_id,
                f"Found {len(conflicts)} conflicts that need manual resolution",
                conflicts
            )
        
        elif current_stage == "NEEDS_RESOLVE" and next_stage != "NEEDS_RESOLVE":
            # 离开 NEEDS_RESOLVE 状态，停止监控
            await self.resolve_watcher.stop_watching(job_id)
        
        # 执行状态转换
        await self.transition_stage(job_id, current_stage, next_stage, result)
```

### 9.5 用户界面交互
```javascript
// 前端 JavaScript 处理用户点击 Continue
async function handleContinueClick(jobId) {
    // 显示加载状态
    showLoading("Checking conflict resolution status...");
    
    try {
        // 调用后端 API 触发立即检查
        const response = await fetch(`/api/jobs/${jobId}/continue`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'}
        });
        
        const result = await response.json();
        
        if (result.conflicts_resolved) {
            showSuccess("Conflicts resolved! Job is proceeding to next stage.");
            // 刷新页面显示新状态
            refreshJobStatus(jobId);
        } else {
            showWarning(`Still have ${result.remaining_conflicts} conflicts to resolve.`);
            // 显示冲突文件列表
            displayConflictsList(result.conflict_files);
        }
    } catch (error) {
        showError("Failed to check conflict status: " + error.message);
    }
}

// 定期刷新页面，显示自动检测的结果
setInterval(async () => {
    const currentStage = document.querySelector('.job-stage').textContent;
    if (currentStage === 'NEEDS_RESOLVE') {
        // 静默刷新状态，如果检测到已解决会自动更新显示
        const status = await fetchJobStatus(jobId);
        if (status.stage !== 'NEEDS_RESOLVE') {
            showInfo("Conflicts have been automatically resolved!");
            location.reload();  // 刷新页面显示新状态
        }
    }
}, 10000);  // 每10秒检查一次前端显示
```

        # ... 其他阶段的判定逻辑
```

## 十、实施计划

### 第一阶段：安全基础 (第1-2周)
1. **TLS 配置**
   - 生成 CA 证书和 Agent/Master 证书
   - 修改通信代码支持 TLS
   - 测试证书轮换流程

2. **身份认证**
   - 实现白名单配置加载
   - 添加 JWT token 生成和验证
   - 实现命令签名机制

3. **审计日志**
   - Bootstrap 命令记录
   - 所有安全事件记录

### 第二阶段：状态管理与重连 (第3-4周)
1. **内存状态管理**
   - 实现线程安全的状态存储
   - 添加状态历史追踪

2. **重连机制**
   - Agent 断线重连协议
   - 命令重发和幂等处理

3. **冲突检测机制**
   - 实现 ResolveWatcher 自动轮询
   - NEEDS_RESOLVE 状态自动检测

### 第三阶段：可观测性 (第5-6周)
1. **结构化日志**
   - 统一日志格式
   - 实现日志轮转

2. **监控指标**
   - 集成 Prometheus
   - 配置 Grafana 仪表板

3. **告警规则**
   - 定义关键告警
   - 集成告警通知

### 第四阶段：高级特性 (第7-8周)
1. **资源隔离**
   - 实现沙箱执行
   - 添加资源限制

2. **自动恢复**
   - 实现重试策略
   - 添加熔断器

3. **性能优化**
   - 连接池管理
   - 负载均衡

## 十一、配置示例

### 11.1 Master 配置
```yaml
# config/master.yaml
server:
  host: "0.0.0.0"
  port: 9090
  ssl:
    enabled: true
    cert_file: "certs/master.crt"
    key_file: "certs/master.key"
    ca_file: "certs/ca.crt"
    verify_client: true

security:
  jwt_secret: "${JWT_SECRET}"
  token_expiry: 3600
  agent_whitelist: "config/agent_whitelist.yaml"

state_management:
  type: "memory"  # 纯内存状态
  resolve_check_interval: 30  # NEEDS_RESOLVE 状态检查间隔（秒）
  max_history_per_job: 100  # 每个任务最多保留的历史记录数

monitoring:
  metrics_port: 9091
  log_level: "INFO"
  log_format: "json"
  
resources:
  max_concurrent_jobs: 10
  max_agents: 100
  connection_timeout: 300
```

### 11.2 Agent 配置
```yaml
# config/agent.yaml
agent:
  id: "${HOSTNAME}-${RANDOM}"
  workspace: "/local_vol1_nobackup/${USER}"
  
server:
  master_host: "p4-master.company.com"
  master_port: 9090
  ssl:
    enabled: true
    cert_file: "certs/agent-${HOSTNAME}.crt"
    key_file: "certs/agent-${HOSTNAME}.key"
    ca_file: "certs/ca.crt"

execution:
  sandbox: true
  resource_limits:
    cpu: "50%"
    memory: "2G"
    disk: "10G"
  timeout_multiplier: 1.5

logging:
  level: "INFO"
  file: "logs/agent.log"
  max_size: "100M"
  max_files: 10
```

## 十二、总结

### 12.1 关键改进
1. **安全性**: 从传输到执行的全链路安全
2. **可靠性**: 内存状态管理和自动恢复
3. **可观测性**: 结构化日志和实时监控
4. **扩展性**: 支持大规模部署
5. **维护性**: 清晰的故障处理流程

### 12.2 预期收益
- **稳定性提升**: 故障自动恢复，减少人工干预
- **安全合规**: 满足企业安全审计要求
- **运维效率**: 问题快速定位和解决
- **用户体验**: 实时状态更新，操作透明

### 12.3 后续优化方向
1. 支持 Kubernetes 部署
2. 集成 CI/CD pipeline
3. 添加 Web UI 的实时推送
4. 支持多地域部署
