"""
AgentServer - Master TCP Server
Listens for Agent connections and forwards events to the JobStateMachine.
"""
import asyncio
import json
from typing import Dict, Optional, List, Any
from datetime import datetime
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AgentServer")

class AgentConnection:
    """Wrapper for a single Agent connection"""
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, agent_id: str):
        self.reader = reader
        self.writer = writer
        self.agent_id = agent_id
        self.hostname: Optional[str] = None
        self.ip: Optional[str] = None
        self.connected_at = datetime.now()
        self.last_heartbeat = datetime.now()
        self.workspace: Optional[str] = None
        
    async def send_message(self, data: dict):
        """Send message to Agent"""
        try:
            message = json.dumps(data) + "\n"
            self.writer.write(message.encode('utf-8'))
            await self.writer.drain()
        except Exception as e:
            logger.error(f"Error sending to {self.agent_id}: {e}")
            raise
    
    async def receive_message(self) -> Optional[dict]:
        """Receive message from Agent"""
        try:
            line = await self.reader.readline()
            if not line:
                return None
            return json.loads(line.decode('utf-8'))
        except Exception as e:
            logger.error(f"Error receiving from {self.agent_id}: {e}")
            return None
    
    def close(self):
        """Close connection"""
        try:
            self.writer.close()
        except Exception:
            pass

class AgentServer:
    """Manages Agent connections"""
    def __init__(self, host: str = "0.0.0.0", port: int = 9090):
        self.host = host
        self.port = port
        self.agents: Dict[str, AgentConnection] = {}  # agent_id -> connection
        self.event_handlers: List[Any] = []
        self.server: Optional[asyncio.AbstractServer] = None
        self._loop = None
        # Track expected agents (for connection monitoring)
        self.expected_agents: Dict[str, dict] = {}  # agent_id -> {"added_at": datetime, "timeout_at": datetime}
        
    def register_event_handler(self, handler):
        """Register an event handler (usually JobStateMachine)"""
        self.event_handlers.append(handler)
    
    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle incoming Agent connection"""
        addr = writer.get_extra_info('peername')
        # Use IP:Port as temporary ID until REGISTER
        temp_id = f"{addr[0]}:{addr[1]}"
        
        logger.info(f"New connection from {temp_id}")
        
        connection = AgentConnection(reader, writer, temp_id)
        agent_id = temp_id
        
        try:
            # Wait for REGISTER message (timeout 5s)
            try:
                register_msg = await asyncio.wait_for(connection.receive_message(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(f"Timeout waiting for REGISTER from {temp_id}")
                return

            if register_msg and register_msg.get("type") == "REGISTER":
                connection.hostname = register_msg.get("hostname")
                connection.ip = register_msg.get("ip")
                connection.workspace = register_msg.get("workspace")
                
                # Use Hostname:IP as stable ID if possible, or fallback
                # In practice, we might want something unique per session or persistent ID
                # For now, let's use IP:Port or Hostname
                agent_id = f"{connection.hostname}:{addr[1]}"
                connection.agent_id = agent_id
                
                self.agents[agent_id] = connection
                logger.info(f"Agent registered: {agent_id} ({connection.workspace})")
                
                # Send ACK
                await connection.send_message({
                    "type": "HANDSHAKE_ACK",
                    "agent_id": agent_id
                })
            else:
                logger.warning(f"Invalid first message from {temp_id}: {register_msg}")
                return
            
            # Message loop
            while True:
                message = await connection.receive_message()
                if not message:
                    break
                
                # Update heartbeat
                if message.get("type") == "HEARTBEAT":
                    connection.last_heartbeat = datetime.now()
                
                # Dispatch to handlers
                for handler in self.event_handlers:
                    # Handlers should be async methods
                    if asyncio.iscoroutinefunction(handler.handle_agent_event):
                        await handler.handle_agent_event(agent_id, message)
                    else:
                        handler.handle_agent_event(agent_id, message)
                    
        except Exception as e:
            logger.error(f"Error handling agent {agent_id}: {e}")
        finally:
            if agent_id in self.agents:
                del self.agents[agent_id]
            connection.close()
            logger.info(f"Agent {agent_id} disconnected")
    
    async def send_to_agent(self, agent_id: str, message: dict):
        """Send message to specific Agent"""
        if agent_id in self.agents:
            await self.agents[agent_id].send_message(message)
        else:
            raise ValueError(f"Agent {agent_id} not connected")
            
    async def broadcast(self, message: dict):
        """Send message to ALL agents"""
        for agent_id in list(self.agents.keys()):
            try:
                await self.agents[agent_id].send_message(message)
            except Exception:
                pass
    
    async def start(self):
        """Start the TCP server"""
        self._loop = asyncio.get_running_loop()
        self.server = await asyncio.start_server(
            self.handle_client, 
            self.host, 
            self.port
        )
        
        logger.info(f"AgentServer listening on {self.host}:{self.port}")
        
        async with self.server:
            await self.server.serve_forever()
    
    def get_connected_agents(self) -> Dict[str, dict]:
        """Get info for all connected agents"""
        return {
            aid: {
                "hostname": conn.hostname,
                "ip": conn.ip,
                "workspace": conn.workspace,
                "connected_at": conn.connected_at.isoformat(),
                "last_heartbeat": conn.last_heartbeat.isoformat()
            }
            for aid, conn in self.agents.items()
        }
    
    def expect_agent(self, agent_hint: str, timeout_seconds: int = 30):
        """Mark an agent as expected to connect soon"""
        now = datetime.now()
        self.expected_agents[agent_hint] = {
            "added_at": now,
            "timeout_at": datetime.fromtimestamp(now.timestamp() + timeout_seconds)
        }
        
    def wait_for_agent(self, agent_hint: str, timeout: float = 5.0) -> Optional[str]:
        """Wait for an expected agent to connect
        Returns actual agent_id if connected, None if timeout
        
        agent_hint can be hostname, FQDN, or IP address - we normalize and check all"""
        import time
        start = time.time()
        
        # Normalize hint: extract base hostname (before first dot)
        hint_base = agent_hint.split('.')[0]
        
        while time.time() - start < timeout:
            # Check if any agent matches the hint
            for aid, conn in self.agents.items():
                matched = False
                
                # 1. Check agent_id contains hint
                if agent_hint in aid:
                    matched = True
                
                # 2. Check hostname (normalized comparison to handle FQDN vs short name)
                if conn.hostname:
                    hostname_base = conn.hostname.split('.')[0]
                    # Match if base hostnames match, or if either is substring of the other
                    if (hint_base == hostname_base or 
                        agent_hint in conn.hostname or 
                        conn.hostname in agent_hint):
                        matched = True
                
                # 3. Check IP matches hint
                if conn.ip and agent_hint == conn.ip:
                    matched = True
                
                if matched:
                    if agent_hint in self.expected_agents:
                        del self.expected_agents[agent_hint]
                    return aid
                    
            time.sleep(0.1)
        return None
        
    def get_agent_status(self) -> dict:
        """Get comprehensive agent status including expected agents"""
        now = datetime.now()
        return {
            "connected": self.get_connected_agents(),
            "expected": {
                hint: {
                    "waiting_since": info["added_at"].isoformat(),
                    "timeout_at": info["timeout_at"].isoformat(),
                    "timed_out": now > info["timeout_at"]
                }
                for hint, info in self.expected_agents.items()
            }
        }

