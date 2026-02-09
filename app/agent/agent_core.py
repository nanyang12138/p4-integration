#!/usr/bin/env python3
"""
P4 Integration Agent - Remote Executor
Connects to Master and executes P4 commands.
Self-contained script for easy deployment.
"""
import asyncio
import json
import sys
import os
import subprocess
import socket
import time
import logging
import traceback
from typing import Optional, Dict

# Setup basic logging immediately
LOG_FILE = f"/tmp/p4_agent_{os.getpid()}.log"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

# Also log to stdout/stderr for development if connected
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger('').addHandler(console)

class P4Agent:
    def __init__(self, master_host: str, master_port: int, workspace: str):
        self.master_host = master_host
        self.master_port = master_port
        self.workspace = workspace
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.running_commands: Dict[str, subprocess.Popen] = {}  # cmd_id -> process
        self.is_running = False

    async def connect(self):
        """Connect to Master with exponential backoff"""
        retry_count = 0
        max_retries = 10
        
        while retry_count < max_retries:
            try:
                logging.info(f"Connecting to Master at {self.master_host}:{self.master_port}...")
                self.reader, self.writer = await asyncio.open_connection(
                    self.master_host, 
                    self.master_port
                )
                logging.info(f"Connected!")
                
                # Send REGISTER message
                hostname = socket.gethostname()
                try:
                    ip = socket.gethostbyname(hostname)
                except:
                    ip = "127.0.0.1"
                    
                await self.send_message({
                    "type": "REGISTER",
                    "hostname": hostname,
                    "ip": ip,
                    "workspace": self.workspace
                })
                
                self.is_running = True
                return True
            except Exception as e:
                retry_count += 1
                wait_time = min(2 ** retry_count, 30)
                logging.error(f"Connection failed: {e}. Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
        
        logging.error("Max retries reached. Giving up.")
        return False
    
    async def send_message(self, data: dict):
        """Send a JSON Lines message"""
        if not self.writer:
            return
        try:
            message = json.dumps(data) + "\n"
            self.writer.write(message.encode('utf-8'))
            await self.writer.drain()
        except Exception as e:
            logging.error(f"Error sending message: {e}")
            self.is_running = False
    
    async def receive_message(self) -> dict:
        """Receive a JSON Lines message"""
        if not self.reader:
            raise ConnectionError("Not connected")
        
        line = await self.reader.readline()
        if not line:
            raise ConnectionError("Connection closed by Master")
        return json.loads(line.decode('utf-8'))
    
    async def handle_exec_cmd(self, data: dict):
        """Execute a shell command"""
        cmd_id = data["cmd_id"]
        command = data["command"]
        cwd = data.get("cwd", self.workspace)
        
        # Ensure workspace directory exists
        if cwd:
            try:
                os.makedirs(cwd, exist_ok=True)
                logging.info(f"Working directory ensured: {cwd}")
            except Exception as e:
                logging.warning(f"Failed to create directory {cwd}: {e}")
        
        # Merge environment variables
        env = os.environ.copy()
        env.update(data.get("env", {}))
        
        logging.info(f"Executing cmd_id={cmd_id} in cwd={cwd}: {command}")
        
        try:
            # Create subprocess
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env
            )
            
            self.running_commands[cmd_id] = process
            
            # Send CMD_STARTED with PID
            await self.send_message({
                "type": "CMD_STARTED",
                "cmd_id": cmd_id,
                "pid": process.pid
            })
            logging.info(f"Command {cmd_id} started with PID {process.pid}")
            
            # Stream stdout and stderr in parallel
            async def stream_output(stream, stream_type):
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    
                    await self.send_message({
                        "type": "LOG",
                        "cmd_id": cmd_id,
                        "stream": stream_type,
                        "data": line.decode('utf-8', errors='replace').rstrip('\n')
                    })
            
            await asyncio.gather(
                stream_output(process.stdout, "stdout"),
                stream_output(process.stderr, "stderr")
            )
            
            exit_code = await process.wait()
            
            # Cleanup
            if cmd_id in self.running_commands:
                del self.running_commands[cmd_id]
            
            # Send CMD_DONE
            await self.send_message({
                "type": "CMD_DONE",
                "cmd_id": cmd_id,
                "exit_code": exit_code
            })
            
            logging.info(f"Command {cmd_id} finished with exit_code={exit_code}")
            
        except Exception as e:
            logging.error(f"Error executing {cmd_id}: {e}")
            await self.send_message({
                "type": "CMD_DONE",
                "cmd_id": cmd_id,
                "exit_code": -1,
                "error": str(e)
            })
    
    async def handle_kill_cmd(self, data: dict):
        """Kill a running command"""
        cmd_id = data["cmd_id"]
        signal_num = data.get("signal", 15)  # SIGTERM
        
        if cmd_id in self.running_commands:
            try:
                process = self.running_commands[cmd_id]
                process.send_signal(signal_num)
                logging.info(f"Sent signal {signal_num} to cmd_id={cmd_id}")
            except Exception as e:
                logging.error(f"Failed to kill cmd_id={cmd_id}: {e}")
    
    async def cleanup_processes(self):
        """Terminate all running subprocesses to prevent zombie processes"""
        if not self.running_commands:
            return
        logging.info(f"Cleaning up {len(self.running_commands)} running process(es)")
        for cmd_id, process in list(self.running_commands.items()):
            try:
                process.terminate()
                logging.info(f"Terminated process for cmd_id={cmd_id} (pid={process.pid})")
            except ProcessLookupError:
                logging.info(f"Process for cmd_id={cmd_id} already exited")
            except Exception as e:
                logging.warning(f"Failed to terminate process for cmd_id={cmd_id}: {e}")
        self.running_commands.clear()

    async def heartbeat_loop(self):
        """Send periodic heartbeats"""
        while self.is_running:
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
        """Main message processing loop"""
        while self.is_running:
            try:
                message = await self.receive_message()
                msg_type = message.get("type")
                
                if msg_type == "EXEC_CMD":
                    asyncio.create_task(self.handle_exec_cmd(message))
                elif msg_type == "KILL_CMD":
                    await self.handle_kill_cmd(message)
                elif msg_type == "SHUTDOWN":
                    logging.info("Received shutdown signal")
                    await self.cleanup_processes()
                    self.is_running = False
                    break
                    
            except ConnectionError:
                logging.info("Connection lost")
                self.is_running = False
                break
            except Exception as e:
                logging.error(f"Error in message loop: {e}")
                self.is_running = False
                break
    
    async def run(self):
        """Main entry point with auto-reconnect"""
        max_reconnects = 5
        reconnect_count = 0
        
        while reconnect_count < max_reconnects:
            # Try to connect
            if not await self.connect():
                reconnect_count += 1
                logging.warning(f"Connection failed, attempt {reconnect_count}/{max_reconnects}")
                if reconnect_count < max_reconnects:
                    await asyncio.sleep(5)
                continue
            
            # Reset counter on successful connection
            reconnect_count = 0
            
            try:
                await asyncio.gather(
                    self.heartbeat_loop(),
                    self.message_loop()
                )
            except Exception as e:
                logging.error(f"Error in main loop: {e}")
            finally:
                # Cleanup running processes to prevent zombies
                await self.cleanup_processes()
                # Cleanup connection
                if self.writer:
                    try:
                        self.writer.close()
                        await self.writer.wait_closed()
                    except Exception:
                        pass
                self.writer = None
                self.reader = None
            
            # If we get here, connection was lost
            if not self.is_running:
                # Graceful shutdown requested
                logging.info("Graceful shutdown")
                break
            
            # Try to reconnect
            reconnect_count += 1
            if reconnect_count < max_reconnects:
                logging.info(f"Connection lost, reconnecting... ({reconnect_count}/{max_reconnects})")
                await asyncio.sleep(5)
                self.is_running = True  # Reset flag for reconnect attempt
        
        logging.info("Agent stopped")

if __name__ == "__main__":
    try:
        logging.info(f"Agent starting with args: {sys.argv}")
        
        if len(sys.argv) < 4:
            logging.error("Usage: python agent_core.py <master_host> <master_port> <workspace>")
            sys.exit(1)
        
        master_host = sys.argv[1]
        master_port = int(sys.argv[2])
        workspace = sys.argv[3]
        
        # Windows compatibility for asyncio loop policy if needed
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        agent = P4Agent(master_host, master_port, workspace)
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.critical(f"Fatal error: {traceback.format_exc()}")
        sys.exit(1)
