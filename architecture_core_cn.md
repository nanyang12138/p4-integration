# P4 集成系统核心改造：从 SSH 阻塞到 Master-Agent 事件驱动

## 一、核心问题

### 当前架构的痛点
1. **SSH 阻塞式执行**：每个 P4 命令通过 SSH 同步执行，主进程被阻塞
2. **PID 轮询监控**：通过 `ps -p {pid}` 查看进程状态，效率低且不可靠
3. **日志混乱**：所有输出混在一起，难以解析和定位问题
4. **状态不透明**：无法实时知道任务执行到哪个阶段

### 目标架构
- **非阻塞执行**：Master 发送命令后立即返回，通过事件获知结果
- **实时状态推送**：Agent 主动推送状态变化和日志
- **清晰的阶段管理**：每个阶段都有明确的开始、执行、结束事件

## 二、最小化实施方案

### 2.1 基础架构
```
┌─────────────┐  TCP Socket   ┌─────────────┐
│   Master    │ ←───────────→ │    Agent    │
│  (原有逻辑)  │   Port 9090   │ (远程执行器) │
└─────────────┘               └─────────────┘
     ↓                              ↑
   发送命令                      执行并推送状态
```

### 2.2 核心组件

#### Master 端（最小改动）
```python
# app/master/agent_client.py
class AgentClient:
    """与 Agent 通信的客户端"""
    
    def __init__(self, host: str, port: int = 9090):
        self.host = host
        self.port = port
        self.socket = None
        self.response_queue = asyncio.Queue()
        
    async def connect(self):
        """建立 TCP 连接"""
        reader, writer = await asyncio.open_connection(self.host, self.port)
        self.reader = reader
        self.writer = writer
        # 启动接收线程
        asyncio.create_task(self._receive_loop())
        
    async def execute_command(self, cmd: str, env: dict = None) -> str:
        """发送命令到 Agent（非阻塞）"""
        cmd_id = str(uuid.uuid4())
        message = {
            "type": "EXEC_CMD",
            "cmd_id": cmd_id,
            "command": cmd,
            "env": env or {}
        }
        
        # 发送命令
        await self._send(message)
        
        # 立即返回命令ID，不等待结果
        return cmd_id
    
    async def _receive_loop(self):
        """接收 Agent 推送的消息"""
        while True:
            message = await self._recv()
            if message["type"] == "CMD_DONE":
                # 命令执行完成，触发状态机转换
                await self.handle_command_done(message)
            elif message["type"] == "LOG":
                # 实时日志，写入文件
                await self.handle_log(message)
```

#### Agent 端（新组件）
```python
# app/agent/core.py
class P4Agent:
    """在远程机器上运行的执行器"""
    
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.current_process = None
        
    async def handle_command(self, cmd_data: dict):
        """执行命令并实时推送状态"""
        cmd_id = cmd_data["cmd_id"]
        command = cmd_data["command"]
        
        # 创建子进程
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.workspace,
            env={**os.environ, **cmd_data.get("env", {})}
        )
        
        self.current_process = process
        
        # 实时转发输出
        async def forward_stream(stream, stream_type):
            async for line in stream:
                await self.send_to_master({
                    "type": "LOG",
                    "cmd_id": cmd_id,
                    "stream": stream_type,
                    "data": line.decode('utf-8', errors='replace')
                })
        
        # 并行读取 stdout 和 stderr
        await asyncio.gather(
            forward_stream(process.stdout, "stdout"),
            forward_stream(process.stderr, "stderr")
        )
        
        # 等待进程结束
        exit_code = await process.wait()
        
        # 发送完成事件
        await self.send_to_master({
            "type": "CMD_DONE",
            "cmd_id": cmd_id,
            "exit_code": exit_code
        })
```

### 2.3 集成到现有代码

#### 修改 p4_client.py
```python
class P4Client:
    def __init__(self, config):
        # ... 原有代码 ...
        if self.exec_mode == "ssh":
            # 新增：使用 Agent 模式
            self.agent_client = AgentClient(self.ssh_host, 9090)
            self.use_agent = True  # 配置开关
    
    async def _run_async(self, args: List[str], input_text: Optional[str] = None):
        """异步执行 P4 命令"""
        if self.use_agent and self.exec_mode == "ssh":
            # 构建命令
            cmd = self._build_command(args)
            
            # 通过 Agent 执行（非阻塞）
            cmd_id = await self.agent_client.execute_command(cmd, self._env())
            
            # 返回命令ID，让上层通过事件处理结果
            return cmd_id
        else:
            # 降级到原有的同步执行
            return self._run(args, input_text)
```

#### 修改 jobs.py
```python
class JobRunner:
    async def _run_job_async(self, job_id: str):
        """使用事件驱动方式运行任务"""
        
        # 状态机定义
        self.state_machine = {
            "INIT": self._do_sync,
            "SYNC": self._do_integrate,
            "INTEGRATE": self._do_resolve_1,
            "RESOLVE_PASS_1": self._do_resolve_2,
            "RESOLVE_PASS_2": self._do_needs_resolve,
            "NEEDS_RESOLVE": self._wait_resolve,
            "RESOLVE_CHECK": self._do_submit,
            "PRE_SUBMIT": self._do_shelve,
            "SHELVE": self._do_name_check,
            "NC_FIX": self._do_push,
            "P4PUSH": self._finish
        }
        
        # 初始状态
        self.job_state[job_id] = "INIT"
        
        # 启动状态机
        await self._execute_stage(job_id)
    
    async def _execute_stage(self, job_id: str):
        """执行当前阶段"""
        current_stage = self.job_state[job_id]
        handler = self.state_machine.get(current_stage)
        
        if handler:
            # 执行阶段处理器（非阻塞）
            cmd_id = await handler(job_id)
            
            # 记录命令ID和阶段的关系
            self.cmd_to_stage[cmd_id] = current_stage
    
    async def handle_command_done(self, message: dict):
        """处理命令完成事件"""
        cmd_id = message["cmd_id"]
        exit_code = message["exit_code"]
        
        # 找到对应的阶段
        stage = self.cmd_to_stage.get(cmd_id)
        job_id = self.stage_to_job[stage]
        
        # 根据结果决定下一步
        if exit_code == 0:
            # 成功，进入下一阶段
            next_stage = self._get_next_stage(stage)
            self.job_state[job_id] = next_stage
            
            # 继续执行
            await self._execute_stage(job_id)
        else:
            # 失败，标记错误
            self.job_state[job_id] = "FAILED"
            self.log_error(job_id, f"Stage {stage} failed with code {exit_code}")
```

### 2.4 部署方案

#### Agent 启动脚本
```python
#!/usr/bin/env python3
# deploy/start_agent.py

import asyncio
import sys
import os

# Agent 代码（内嵌，避免文件传输）
AGENT_CODE = '''
import asyncio
import json
import subprocess
import os

class P4Agent:
    # ... Agent 实现代码 ...

if __name__ == "__main__":
    agent = P4Agent(sys.argv[1] if len(sys.argv) > 1 else os.getcwd())
    asyncio.run(agent.start())
'''

# 通过 SSH 启动 Agent
def deploy_agent(ssh_host: str, workspace: str):
    import subprocess
    
    # 将 Agent 代码通过 SSH 传输并执行
    ssh_command = f"""
    ssh {ssh_host} 'cd {workspace} && python3 -c "{AGENT_CODE}" {workspace} &'
    """
    
    subprocess.run(ssh_command, shell=True)
```

## 三、实施步骤

### 第一步：实现基础通信（1-2天）
1. 创建 `app/agent/core.py`：Agent 核心逻辑
2. 创建 `app/master/agent_client.py`：Master 端客户端
3. 实现基本的消息收发

### 第二步：集成到 P4Client（2-3天）
1. 修改 `p4_client.py`，添加异步执行方法
2. 添加配置开关，支持新旧模式切换
3. 测试基本 P4 命令执行

### 第三步：改造任务执行器（3-4天）
1. 修改 `jobs.py`，实现事件驱动的状态机
2. 处理各种 P4 命令的结果解析
3. 实现 NEEDS_RESOLVE 的自动检测

### 第四步：优化和调试（2-3天）
1. 添加错误处理和重连机制
2. 优化日志输出格式
3. 性能调优

## 四、关键优势

### 4.1 最小化改动
- 保留原有的业务逻辑
- 只改变执行方式，不改变执行内容
- 支持渐进式迁移

### 4.2 立即可见的改善
- **非阻塞执行**：Web UI 不会卡死
- **实时日志**：看到命令执行的实时输出
- **清晰的状态**：知道当前在哪个阶段

### 4.3 易于调试
- 每个组件职责单一
- 通信协议简单明了
- 支持降级到原有模式

## 五、风险控制

### 5.1 降级方案
```python
# 在配置中添加开关
use_agent_mode: false  # 关闭后退回到原有的 SSH 阻塞模式
```

### 5.2 监控要点
- Agent 进程存活状态
- 网络连接稳定性
- 命令执行超时

### 5.3 错误处理
```python
class AgentClient:
    async def execute_command_with_timeout(self, cmd: str, timeout: int = 3600):
        """带超时的命令执行"""
        try:
            cmd_id = await self.execute_command(cmd)
            
            # 等待结果，但设置超时
            result = await asyncio.wait_for(
                self.wait_for_result(cmd_id), 
                timeout=timeout
            )
            return result
        except asyncio.TimeoutError:
            # 超时后尝试杀死远程进程
            await self.kill_command(cmd_id)
            raise
```

## 六、总结

这个方案专注于解决核心问题：
1. **非阻塞执行**：Master 不再等待命令完成
2. **事件驱动**：通过事件触发状态转换
3. **实时反馈**：日志和状态实时推送

通过最小化的改动，我们可以在 1-2 周内完成核心功能，然后再逐步增强（如安全、持久化等）。
