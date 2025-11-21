# P4 集成系统 - Master-Agent 架构实施文档

## 一、架构总览

基于 architecture_v2.md 的设计，本文档提供完整的实施方案，**完全替换**现有的 SSH 阻塞模式。

### 1.1 核心改变
```
【旧模式】
Flask → P4Client → SSH 阻塞执行 → 等待结果 → 返回
         ↓
      进程卡死，界面无响应

【新模式】
Flask → Master.dispatch_job() → 立即返回
         ↓
      AgentServer 监听 9090
         ↓
      Agent 连接并执行 → 实时推送事件
         ↓
      JobStateMachine 处理事件 → 状态转换
```

### 1.2 文件结构
```
p4-integration/
├── app/
│   ├── master/
│   │   ├── __init__.py
│   │   ├── agent_server.py      # TCP 服务器（监听 9090）
│   │   ├── job_state_machine.py # 状态机核心
│   │   └── bootstrapper.py      # Agent 自动部署
│   ├── agent/
│   │   ├── agent_core.py        # Agent 完整代码（单文件）
│   │   └── protocol.py          # 通信协议定义
│   ├── p4_client.py             # 【修改】移除 SSH 执行逻辑
│   ├── jobs.py                  # 【修改】改为事件驱动
│   └── api.py                   # 【修改】WebSocket 实时日志
└── config.yaml                   # 【修改】移除 exec_mode
```

## 二、核心组件实现

### 2.1 Agent 核心（app/agent/agent_core.py）
这是在远程机器上运行的完整 Agent 代码（单文件，便于 Bootstrap）。

```python
#!/usr/bin/env python3
"""
P4 Integration Agent - 远程执行器
连接到 Master 并执行 P4 命令
"""
import asyncio
import json
import sys
import os
import subprocess
import socket
import time
from typing import Optional

class P4Agent:
    def __init__(self, master_host: str, master_port: int, workspace: str):
        self.master_host = master_host
        self.master_port = master_port
        self.workspace = workspace
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.current_process: Optional[subprocess.Popen] = None
        self.running_commands = {}  # cmd_id -> process
        
    async def connect(self):
        """连接到 Master"""
        retry_count = 0
        while retry_count < 5:
            try:
                self.reader, self.writer = await asyncio.open_connection(
                    self.master_host, 
                    self.master_port
                )
                print(f"[Agent] Connected to Master at {self.master_host}:{self.master_port}")
                
                # 发送注册信息
                await self.send_message({
                    "type": "REGISTER",
                    "hostname": socket.gethostname(),
                    "ip": socket.gethostbyname(socket.gethostname()),
                    "workspace": self.workspace
                })
                
                return True
            except Exception as e:
                retry_count += 1
                wait_time = min(2 ** retry_count, 30)
                print(f"[Agent] Connection failed: {e}, retry in {wait_time}s")
                await asyncio.sleep(wait_time)
        
        return False
    
    async def send_message(self, data: dict):
        """发送 JSON Lines 消息"""
        message = json.dumps(data) + "\n"
        self.writer.write(message.encode('utf-8'))
        await self.writer.drain()
    
    async def receive_message(self) -> dict:
        """接收 JSON Lines 消息"""
        line = await self.reader.readline()
        if not line:
            raise ConnectionError("Connection closed by Master")
        return json.loads(line.decode('utf-8'))
    
    async def handle_exec_cmd(self, data: dict):
        """执行命令"""
        cmd_id = data["cmd_id"]
        command = data["command"]
        cwd = data.get("cwd", self.workspace)
        env = {**os.environ, **data.get("env", {})}
        
        print(f"[Agent] Executing cmd_id={cmd_id}: {command}")
        
        try:
            # 创建子进程
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env
            )
            
            self.running_commands[cmd_id] = process
            
            # 并行读取 stdout 和 stderr
            async def stream_output(stream, stream_type):
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    
                    # 实时发送日志
                    await self.send_message({
                        "type": "LOG",
                        "cmd_id": cmd_id,
                        "stream": stream_type,
                        "data": line.decode('utf-8', errors='replace').rstrip('\n')
                    })
            
            # 同时读取两个流
            await asyncio.gather(
                stream_output(process.stdout, "stdout"),
                stream_output(process.stderr, "stderr")
            )
            
            # 等待进程结束
            exit_code = await process.wait()
            
            # 清理
            del self.running_commands[cmd_id]
            
            # 发送完成事件
            await self.send_message({
                "type": "CMD_DONE",
                "cmd_id": cmd_id,
                "exit_code": exit_code
            })
            
            print(f"[Agent] Command {cmd_id} finished with exit_code={exit_code}")
            
        except Exception as e:
            print(f"[Agent] Error executing {cmd_id}: {e}")
            await self.send_message({
                "type": "CMD_DONE",
                "cmd_id": cmd_id,
                "exit_code": -1,
                "error": str(e)
            })
    
    async def handle_kill_cmd(self, data: dict):
        """杀死正在运行的命令"""
        cmd_id = data["cmd_id"]
        signal_num = data.get("signal", 15)  # 默认 SIGTERM
        
        if cmd_id in self.running_commands:
            process = self.running_commands[cmd_id]
            process.send_signal(signal_num)
            print(f"[Agent] Sent signal {signal_num} to cmd_id={cmd_id}")
    
    async def heartbeat_loop(self):
        """心跳循环"""
        while True:
            try:
                await asyncio.sleep(5)
                await self.send_message({
                    "type": "HEARTBEAT",
                    "timestamp": time.time(),
                    "active_commands": len(self.running_commands)
                })
            except Exception:
                break
    
    async def message_loop(self):
        """消息处理循环"""
        while True:
            try:
                message = await self.receive_message()
                msg_type = message.get("type")
                
                if msg_type == "EXEC_CMD":
                    # 异步执行命令
                    asyncio.create_task(self.handle_exec_cmd(message))
                elif msg_type == "KILL_CMD":
                    await self.handle_kill_cmd(message)
                elif msg_type == "SHUTDOWN":
                    print("[Agent] Received shutdown signal")
                    break
                    
            except ConnectionError:
                print("[Agent] Connection lost")
                break
            except Exception as e:
                print(f"[Agent] Error in message loop: {e}")
                break
    
    async def run(self):
        """主运行循环"""
        if not await self.connect():
            print("[Agent] Failed to connect to Master")
            return
        
        # 启动心跳和消息处理
        await asyncio.gather(
            self.heartbeat_loop(),
            self.message_loop()
        )
        
        # 清理
        self.writer.close()
        await self.writer.wait_closed()

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python agent_core.py <master_host> <master_port> <workspace>")
        sys.exit(1)
    
    master_host = sys.argv[1]
    master_port = int(sys.argv[2])
    workspace = sys.argv[3]
    
    agent = P4Agent(master_host, master_port, workspace)
    asyncio.run(agent.run())
```

### 2.2 Agent 服务器（app/master/agent_server.py）
Master 端监听 9090 端口，管理 Agent 连接。

```python
"""
AgentServer - Master 端 TCP 服务器
监听 Agent 连接并转发事件到 JobStateMachine
"""
import asyncio
import json
from typing import Dict, Optional
from datetime import datetime

class AgentConnection:
    """单个 Agent 连接的包装"""
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, agent_id: str):
        self.reader = reader
        self.writer = writer
        self.agent_id = agent_id
        self.hostname = None
        self.ip = None
        self.connected_at = datetime.now()
        self.last_heartbeat = datetime.now()
        
    async def send_message(self, data: dict):
        """发送消息到 Agent"""
        message = json.dumps(data) + "\n"
        self.writer.write(message.encode('utf-8'))
        await self.writer.drain()
    
    async def receive_message(self) -> Optional[dict]:
        """接收消息"""
        line = await self.reader.readline()
        if not line:
            return None
        return json.loads(line.decode('utf-8'))
    
    def close(self):
        """关闭连接"""
        self.writer.close()

class AgentServer:
    """Agent 连接管理服务器"""
    def __init__(self, host: str = "0.0.0.0", port: int = 9090):
        self.host = host
        self.port = port
        self.agents: Dict[str, AgentConnection] = {}  # agent_id -> connection
        self.event_handlers = []  # 事件处理器列表
        self.server = None
        
    def register_event_handler(self, handler):
        """注册事件处理器（通常是 JobStateMachine）"""
        self.event_handlers.append(handler)
    
    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """处理单个 Agent 连接"""
        addr = writer.get_extra_info('peername')
        agent_id = f"{addr[0]}:{addr[1]}"
        
        print(f"[AgentServer] New connection from {agent_id}")
        
        connection = AgentConnection(reader, writer, agent_id)
        
        try:
            # 等待注册消息
            register_msg = await connection.receive_message()
            if register_msg and register_msg.get("type") == "REGISTER":
                connection.hostname = register_msg.get("hostname")
                connection.ip = register_msg.get("ip")
                self.agents[agent_id] = connection
                
                print(f"[AgentServer] Agent registered: {connection.hostname} ({connection.ip})")
                
                # 发送确认
                await connection.send_message({
                    "type": "HANDSHAKE_ACK",
                    "agent_id": agent_id
                })
            
            # 消息循环
            while True:
                message = await connection.receive_message()
                if not message:
                    break
                
                # 更新心跳时间
                if message.get("type") == "HEARTBEAT":
                    connection.last_heartbeat = datetime.now()
                
                # 分发事件到所有处理器
                for handler in self.event_handlers:
                    await handler.handle_agent_event(agent_id, message)
                    
        except Exception as e:
            print(f"[AgentServer] Error handling agent {agent_id}: {e}")
        finally:
            # 清理连接
            if agent_id in self.agents:
                del self.agents[agent_id]
            connection.close()
            print(f"[AgentServer] Agent {agent_id} disconnected")
    
    async def send_to_agent(self, agent_id: str, message: dict):
        """发送消息到指定 Agent"""
        if agent_id in self.agents:
            await self.agents[agent_id].send_message(message)
        else:
            raise ValueError(f"Agent {agent_id} not connected")
    
    async def start(self):
        """启动服务器"""
        self.server = await asyncio.start_server(
            self.handle_client, 
            self.host, 
            self.port
        )
        
        print(f"[AgentServer] Listening on {self.host}:{self.port}")
        
        async with self.server:
            await self.server.serve_forever()
    
    def get_connected_agents(self) -> Dict[str, dict]:
        """获取所有已连接的 Agent 信息"""
        return {
            agent_id: {
                "hostname": conn.hostname,
                "ip": conn.ip,
                "connected_at": conn.connected_at.isoformat(),
                "last_heartbeat": conn.last_heartbeat.isoformat()
            }
            for agent_id, conn in self.agents.items()
        }
```

### 2.3 状态机（app/master/job_state_machine.py）
业务逻辑核心，处理 Agent 事件并决定下一步。

```python
"""
JobStateMachine - P4 集成任务状态机
基于 Agent 事件驱动状态转换
"""
import asyncio
import re
from typing import Dict, Optional
from datetime import datetime
from enum import Enum

class Stage(Enum):
    """任务阶段枚举"""
    INIT = "INIT"
    SYNC = "SYNC"
    INTEGRATE = "INTEGRATE"
    RESOLVE_PASS_1 = "RESOLVE_PASS_1"
    RESOLVE_PASS_2 = "RESOLVE_PASS_2"
    RESOLVE_CHECK = "RESOLVE_CHECK"
    NEEDS_RESOLVE = "NEEDS_RESOLVE"
    PRE_SUBMIT = "PRE_SUBMIT"
    SHELVE = "SHELVE"
    NC_FIX = "NC_FIX"
    P4PUSH = "P4PUSH"
    DONE = "DONE"
    ERROR = "ERROR"

class JobStateMachine:
    """任务状态机"""
    def __init__(self, agent_server):
        self.agent_server = agent_server
        self.jobs: Dict[str, dict] = {}  # job_id -> job_info
        self.cmd_to_job: Dict[str, str] = {}  # cmd_id -> job_id
        self.logs: Dict[str, list] = {}  # job_id -> [log_lines]
        
        # 注册为 Agent 事件处理器
        agent_server.register_event_handler(self)
    
    def create_job(self, job_id: str, agent_id: str, spec: dict) -> dict:
        """创建新任务"""
        job = {
            "job_id": job_id,
            "agent_id": agent_id,
            "spec": spec,
            "stage": Stage.INIT.value,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "error": None,
            "current_cmd_id": None
        }
        self.jobs[job_id] = job
        self.logs[job_id] = []
        return job
    
    async def start_job(self, job_id: str):
        """启动任务（从 SYNC 阶段开始）"""
        if job_id not in self.jobs:
            raise ValueError(f"Job {job_id} not found")
        
        await self.transition_to(job_id, Stage.SYNC)
    
    async def transition_to(self, job_id: str, next_stage: Stage):
        """状态转换并执行对应命令"""
        job = self.jobs[job_id]
        old_stage = job["stage"]
        job["stage"] = next_stage.value
        job["updated_at"] = datetime.now().isoformat()
        
        print(f"[StateMachine] Job {job_id}: {old_stage} → {next_stage.value}")
        
        # 根据新阶段执行对应命令
        command = self._get_stage_command(job, next_stage)
        if command:
            await self._execute_command(job_id, command)
        elif next_stage == Stage.NEEDS_RESOLVE:
            # 等待用户介入
            print(f"[StateMachine] Job {job_id} waiting for manual conflict resolution")
    
    def _get_stage_command(self, job: dict, stage: Stage) -> Optional[str]:
        """根据阶段获取要执行的命令"""
        spec = job["spec"]
        
        commands = {
            Stage.SYNC: f"source {spec['init_script']} && bootenv && p4w sync",
            Stage.INTEGRATE: f"p4 integrate -b {spec['branch_spec']} {spec['path']}",
            Stage.RESOLVE_PASS_1: "p4 resolve -am",
            Stage.RESOLVE_PASS_2: "p4 resolve -am",
            Stage.RESOLVE_CHECK: "p4 resolve -n",
            Stage.PRE_SUBMIT: spec.get('pre_submit_hook'),  # 可选
            Stage.SHELVE: f"p4 shelve -f -c {spec['changelist']}",
            Stage.NC_FIX: spec.get('name_check_fix_script'),  # 如果存在
            Stage.P4PUSH: f"p4push -c {spec['changelist']}"
        }
        
        return commands.get(stage)
    
    async def _execute_command(self, job_id: str, command: str):
        """通过 Agent 执行命令"""
        import uuid
        
        job = self.jobs[job_id]
        cmd_id = str(uuid.uuid4())
        
        job["current_cmd_id"] = cmd_id
        self.cmd_to_job[cmd_id] = job_id
        
        # 发送执行命令到 Agent
        await self.agent_server.send_to_agent(job["agent_id"], {
            "type": "EXEC_CMD",
            "cmd_id": cmd_id,
            "command": command,
            "cwd": job["spec"]["workspace"],
            "env": job["spec"].get("env", {})
        })
        
        print(f"[StateMachine] Sent command to agent: {command}")
    
    async def handle_agent_event(self, agent_id: str, message: dict):
        """处理 Agent 发来的事件"""
        msg_type = message.get("type")
        
        if msg_type == "LOG":
            await self._handle_log(message)
        elif msg_type == "CMD_DONE":
            await self._handle_cmd_done(message)
    
    async def _handle_log(self, message: dict):
        """处理日志事件"""
        cmd_id = message["cmd_id"]
        
        if cmd_id in self.cmd_to_job:
            job_id = self.cmd_to_job[cmd_id]
            log_line = {
                "stream": message["stream"],
                "data": message["data"],
                "timestamp": datetime.now().isoformat()
            }
            self.logs[job_id].append(log_line)
            
            # TODO: 通过 WebSocket 实时推送到前端
    
    async def _handle_cmd_done(self, message: dict):
        """处理命令完成事件"""
        cmd_id = message["cmd_id"]
        exit_code = message["exit_code"]
        
        if cmd_id not in self.cmd_to_job:
            return
        
        job_id = self.cmd_to_job[cmd_id]
        job = self.jobs[job_id]
        current_stage = Stage(job["stage"])
        
        print(f"[StateMachine] Command {cmd_id} finished with exit_code={exit_code}")
        
        # 根据当前阶段和结果决定下一步
        next_stage = await self._decide_next_stage(job_id, current_stage, exit_code)
        
        if next_stage:
            await self.transition_to(job_id, next_stage)
    
    async def _decide_next_stage(self, job_id: str, current_stage: Stage, exit_code: int) -> Optional[Stage]:
        """决定下一个阶段"""
        
        # 失败处理
        if exit_code != 0 and current_stage not in [Stage.RESOLVE_PASS_1, Stage.RESOLVE_PASS_2]:
            return Stage.ERROR
        
        # 状态转换逻辑（基于 architecture_v2.md 的表格）
        transitions = {
            Stage.SYNC: Stage.INTEGRATE,
            Stage.INTEGRATE: Stage.RESOLVE_PASS_1,
            Stage.RESOLVE_PASS_1: Stage.RESOLVE_PASS_2,
            Stage.RESOLVE_PASS_2: Stage.RESOLVE_CHECK,
            Stage.RESOLVE_CHECK: self._check_resolve_output(job_id),
            Stage.PRE_SUBMIT: Stage.SHELVE,
            Stage.SHELVE: self._check_shelve_output(job_id),
            Stage.NC_FIX: Stage.SHELVE,  # 重试 shelve
            Stage.P4PUSH: Stage.DONE
        }
        
        return transitions.get(current_stage)
    
    def _check_resolve_output(self, job_id: str) -> Stage:
        """检查 resolve -n 的输出判断是否有冲突"""
        logs = self.logs.get(job_id, [])
        output = "\n".join([log["data"] for log in logs if log["stream"] == "stdout"])
        
        if "No file(s) to resolve" in output or not output.strip():
            # 无冲突
            return Stage.PRE_SUBMIT
        elif "merging" in output or "resolve skipped" in output:
            # 有冲突
            return Stage.NEEDS_RESOLVE
        else:
            return Stage.PRE_SUBMIT  # 默认继续
    
    def _check_shelve_output(self, job_id: str) -> Stage:
        """检查 shelve 的输出判断是否需要 name_check 修复"""
        logs = self.logs.get(job_id, [])
        stderr = "\n".join([log["data"] for log in logs if log["stream"] == "stderr"])
        
        if "name_check" in stderr.lower():
            return Stage.NC_FIX
        else:
            return Stage.P4PUSH
    
    async def user_continue(self, job_id: str):
        """用户手动点击 Continue（从 NEEDS_RESOLVE 继续）"""
        job = self.jobs.get(job_id)
        if job and job["stage"] == Stage.NEEDS_RESOLVE.value:
            await self.transition_to(job_id, Stage.RESOLVE_CHECK)
    
    def get_job(self, job_id: str) -> Optional[dict]:
        """获取任务信息"""
        return self.jobs.get(job_id)
    
    def get_job_logs(self, job_id: str) -> list:
        """获取任务日志"""
        return self.logs.get(job_id, [])
```

### 2.4 自动部署器（app/master/bootstrapper.py）
通过 SSH 自动启动远程 Agent。

```python
"""
Bootstrapper - Agent 自动部署工具
通过 SSH 将 Agent 代码注入远程机器内存运行
"""
import paramiko
import base64
import os

class Bootstrapper:
    """Agent 部署器"""
    def __init__(self, ssh_config: dict):
        self.ssh_host = ssh_config["host"]
        self.ssh_user = ssh_config["user"]
        self.ssh_key_path = ssh_config.get("key_path")
        
    def deploy_agent(self, master_host: str, master_port: int, workspace: str) -> str:
        """
        在远程机器上启动 Agent
        返回 agent_id
        """
        # 读取 Agent 代码
        agent_code_path = os.path.join(os.path.dirname(__file__), "../agent/agent_core.py")
        with open(agent_code_path, 'r') as f:
            agent_code = f.read()
        
        # Base64 编码（避免引号转义问题）
        agent_code_b64 = base64.b64encode(agent_code.encode('utf-8')).decode('ascii')
        
        # 构建启动命令
        start_command = f"""
nohup python3 -c "
import base64, sys
exec(base64.b64decode('{agent_code_b64}'))
" {master_host} {master_port} {workspace} > /tmp/p4agent.log 2>&1 &
echo $!
"""
        
        # 通过 SSH 执行
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            ssh.connect(
                self.ssh_host,
                username=self.ssh_user,
                key_filename=self.ssh_key_path
            )
            
            stdin, stdout, stderr = ssh.exec_command(start_command)
            pid = stdout.read().decode().strip()
            
            print(f"[Bootstrapper] Agent started on {self.ssh_host} with PID {pid}")
            
            # 生成 agent_id
            agent_id = f"{self.ssh_host}:{pid}"
            return agent_id
            
        finally:
            ssh.close()
```

## 三、集成到现有代码

### 3.1 修改 app/__init__.py
启动 AgentServer 和 StateMachine。

```python
# app/__init__.py
from flask import Flask
import asyncio
import threading

from app.master.agent_server import AgentServer
from app.master.job_state_machine import JobStateMachine

# 全局实例
agent_server = None
state_machine = None

def create_app(config_name='default'):
    app = Flask(__name__)
    
    # 原有配置...
    
    # 启动 Agent 服务器（在后台线程）
    global agent_server, state_machine
    agent_server = AgentServer(host="0.0.0.0", port=9090)
    state_machine = JobStateMachine(agent_server)
    
    def run_agent_server():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(agent_server.start())
    
    server_thread = threading.Thread(target=run_agent_server, daemon=True)
    server_thread.start()
    
    # 注册路由...
    from app import api
    app.register_blueprint(api.bp)
    
    return app
```

### 3.2 修改 app/api.py
暴露新的 API 接口。

```python
# app/api.py
from flask import Blueprint, jsonify, request
from app import state_machine, agent_server
from app.master.bootstrapper import Bootstrapper

bp = Blueprint('api', __name__, url_prefix='/api')

@bp.route('/jobs', methods=['POST'])
def create_job():
    """创建并启动新任务"""
    data = request.json
    job_id = data["job_id"]
    spec = data["spec"]
    
    # 部署 Agent
    bootstrapper = Bootstrapper(data["ssh_config"])
    agent_id = bootstrapper.deploy_agent(
        master_host=data["master_host"],
        master_port=9090,
        workspace=spec["workspace"]
    )
    
    # 创建任务
    job = state_machine.create_job(job_id, agent_id, spec)
    
    # 等待 Agent 连接（最多等10秒）
    import time
    for _ in range(10):
        if agent_id in agent_server.agents:
            break
        time.sleep(1)
    
    # 启动任务
    import asyncio
    asyncio.run_coroutine_threadsafe(
        state_machine.start_job(job_id),
        agent_server.server._loop
    )
    
    return jsonify({"job_id": job_id, "status": "started"})

@bp.route('/jobs/<job_id>', methods=['GET'])
def get_job(job_id):
    """获取任务状态"""
    job = state_machine.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)

@bp.route('/jobs/<job_id>/logs', methods=['GET'])
def get_job_logs(job_id):
    """获取任务日志"""
    logs = state_machine.get_job_logs(job_id)
    return jsonify({"logs": logs})

@bp.route('/jobs/<job_id>/continue', methods=['POST'])
def continue_job(job_id):
    """用户手动继续任务（NEEDS_RESOLVE）"""
    import asyncio
    asyncio.run_coroutine_threadsafe(
        state_machine.user_continue(job_id),
        agent_server.server._loop
    )
    return jsonify({"status": "continued"})

@bp.route('/agents', methods=['GET'])
def get_agents():
    """获取所有已连接的 Agent"""
    return jsonify(agent_server.get_connected_agents())
```

### 3.3 简化 app/p4_client.py
移除所有 SSH 执行逻辑（现在由 Agent 负责）。

```python
# app/p4_client.py
# 大部分 SSH 相关代码可以删除
# 保留配置解析和命令构建逻辑即可

class P4Client:
    def __init__(self, config):
        self.p4_port = config['p4']['port']
        self.p4_user = config['p4']['user']
        # 移除 exec_mode, ssh_host 等字段
    
    def build_command(self, args: List[str]) -> str:
        """构建 P4 命令字符串（供 Agent 执行）"""
        cmd_parts = [f"p4 -p {self.p4_port} -u {self.p4_user}"]
        cmd_parts.extend(args)
        return " ".join(cmd_parts)
```

### 3.4 简化 app/jobs.py
移除 PID 轮询逻辑，改为依赖 StateMachine。

```python
# app/jobs.py
# 原来的 _run_job 逻辑可以大幅简化
# 只需要准备 spec 并调用 state_machine.create_job()

def submit_job(spec_data):
    """提交新任务"""
    job_id = generate_job_id()
    
    # 准备 spec
    spec = {
        "workspace": spec_data["workspace"],
        "branch_spec": spec_data["branch_spec"],
        "changelist": spec_data["changelist"],
        # ... 其他参数
    }
    
    # 通过 API 创建任务
    # （实际调用会通过 Flask 请求或直接调用 state_machine）
    
    return job_id
```

## 四、实施步骤

### 阶段 1：基础框架（2-3天）
1. 创建 `app/agent/agent_core.py`
2. 创建 `app/master/agent_server.py`
3. 测试基本的连接和消息收发

### 阶段 2：状态机实现（3-4天）
1. 创建 `app/master/job_state_machine.py`
2. 实现完整的状态转换逻辑
3. 集成到 Flask 应用

### 阶段 3：自动部署（1-2天）
1. 创建 `app/master/bootstrapper.py`
2. 测试 SSH 自动启动 Agent

### 阶段 4：前端集成（2-3天）
1. 修改 API 接口
2. 添加 WebSocket 实时日志推送
3. 更新 UI 显示

### 阶段 5：测试与优化（3-5天）
1. 端到端测试完整流程
2. 错误处理和边界情况
3. 性能优化

**总计：11-17 天**

## 五、配置变更

### config.yaml
```yaml
# 移除 exec_mode 字段
p4:
  port: "ssl:perforce.company.com:1666"
  user: "nanyang2"
  bin: "/tool/pandora64/bin/p4"

# 新增 Agent 配置
agent:
  master_host: "192.168.1.100"  # Master 机器的 IP
  master_port: 9090
  
ssh:
  host: "srdcws990"
  user: "nanyang2"
  key_path: "~/.ssh/id_rsa"

workspace:
  root: "/local_vol1_nobackup/nanyang/gfxip_unified/main_ws2"
```

## 六、优势总结

1. **完全非阻塞**：Web 界面永不卡死
2. **实时状态**：清楚知道任务进展
3. **日志清晰**：stdout/stderr 分离，实时流式显示
4. **无残留**：Agent 任务结束后自动退出
5. **易于扩展**：可轻松添加新的阶段或命令

## 七、注意事项

1. **防火墙**：确保远程机器可以访问 Master 的 9090 端口
2. **网络稳定性**：Agent 有断线重连机制，但长时间断连会导致任务失败
3. **资源消耗**：每个 Agent 约 15MB 内存，正常情况下可忽略
4. **日志存储**：实时日志会存储在内存和文件中，大任务注意清理

