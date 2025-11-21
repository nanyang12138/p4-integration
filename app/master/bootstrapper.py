"""
Bootstrapper - Agent Auto-Deployment
Deploys and starts the Agent on remote machine via SSH, or locally.
"""
import paramiko
import base64
import os
import logging
import time
import subprocess
import sys

logger = logging.getLogger("Bootstrapper")

class Bootstrapper:
    def __init__(self, ssh_config: dict):
        self.ssh_host = ssh_config.get("host", "localhost")
        self.ssh_user = ssh_config.get("user")
        self.ssh_key_path = ssh_config.get("key_path")
        self.ssh_password = ssh_config.get("password")
        self.ssh_port = ssh_config.get("port", 22)
        
    def deploy_agent(self, master_host: str, master_port: int, workspace: str, agent_server) -> tuple[str, str]:
        """
        Deploy Agent via SSH or Locally.
        Returns (agent_pid, agent_hint) if successful.
        """
        # 1. Read Agent Source Code
        current_dir = os.path.dirname(os.path.abspath(__file__))
        agent_path = os.path.join(current_dir, "../agent/agent_core.py")
        
        # Check for Local Mode
        is_local = self.ssh_host in ["localhost", "127.0.0.1", "0.0.0.0"]
        
        # Determine agent hint for connection tracking
        agent_hint = self.ssh_host
        
        # Mark this agent as expected
        agent_server.expect_agent(agent_hint, timeout_seconds=30)
        
        logger.info(f"Deploying agent to {self.ssh_host} (Local: {is_local})...")

        if is_local:
            # --- LOCAL DEPLOYMENT ---
            try:
                # Launch agent_core.py directly using current python interpreter
                # We run it as a subprocess.
                
                cmd = [sys.executable, agent_path, master_host, str(master_port), workspace]
                
                # Start process detached/background? 
                # For local dev, subprocess.Popen is fine. It continues running after we return.
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL, 
                    stderr=subprocess.DEVNULL,
                    # On Windows, creationflags=subprocess.CREATE_NEW_CONSOLE might be needed 
                    # if we want it fully detached, but for now standard Popen is ok.
                    start_new_session=True if sys.platform != 'win32' else False
                )
                
                logger.info(f"Local Agent started with PID {process.pid}")
                return str(process.pid), agent_hint
                
            except Exception as e:
                logger.error(f"Local deployment failed: {e}")
                raise

        else:
            # --- SSH DEPLOYMENT ---
            try:
                with open(agent_path, 'r') as f:
                    agent_code = f.read()
            except FileNotFoundError:
                logger.error(f"Agent source not found at {agent_path}")
                raise

            # Prepare Payload
            agent_code_b64 = base64.b64encode(agent_code.encode('utf-8')).decode('ascii')
            
            # Construct Remote Command
            remote_cmd = f"""
nohup python3 -c "import base64, sys; exec(base64.b64decode('{agent_code_b64}'))" {master_host} {master_port} {workspace} > /dev/null 2>&1 & echo $!
"""
            
            # Execute via SSH
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            try:
                connect_kwargs = {
                    "hostname": self.ssh_host,
                    "username": self.ssh_user,
                    "port": self.ssh_port,
                    "timeout": 10
                }
                
                # Try key-based auth first if configured
                if self.ssh_key_path:
                    key_path = os.path.expanduser(self.ssh_key_path)
                    if os.path.exists(key_path):
                        connect_kwargs["key_filename"] = key_path
                        logger.info(f"Using SSH key: {key_path}")
                    else:
                        logger.warning(f"SSH key not found: {key_path}")
                
                # Add password as fallback if provided
                if self.ssh_password:
                    connect_kwargs["password"] = self.ssh_password
                    logger.info("Password authentication configured as fallback")
                
                # Attempt connection
                try:
                    client.connect(**connect_kwargs)
                except paramiko.AuthenticationException as auth_err:
                    logger.error(f"SSH Authentication failed: {auth_err}")
                    
                    # Provide helpful error message
                    error_msg = f"SSH authentication failed for {self.ssh_user}@{self.ssh_host}\n"
                    
                    if self.ssh_key_path:
                        key_path = os.path.expanduser(self.ssh_key_path)
                        if not os.path.exists(key_path):
                            error_msg += f"- SSH key file not found: {key_path}\n"
                            error_msg += f"- Generate key: ssh-keygen -t rsa -f {key_path}\n"
                        else:
                            error_msg += f"- SSH key rejected: {key_path}\n"
                            error_msg += f"- Copy key to remote: ssh-copy-id -i {key_path} {self.ssh_user}@{self.ssh_host}\n"
                    
                    if not self.ssh_password:
                        error_msg += "- No password configured in config.yaml\n"
                        error_msg += "- Add 'password: YOUR_PASSWORD' under 'ssh:' section\n"
                    
                    raise RuntimeError(error_msg) from auth_err
                
                stdin, stdout, stderr = client.exec_command(remote_cmd)
                
                pid = stdout.read().decode().strip()
                err = stderr.read().decode().strip()
                
                if not pid:
                    logger.error(f"Failed to start agent. Stderr: {err}")
                    raise RuntimeError(f"Agent start failed: {err}")
                    
                logger.info(f"SSH Agent started with PID {pid} on {self.ssh_host}")
                return pid, agent_hint
                
            except Exception as e:
                logger.error(f"SSH deployment failed: {e}")
                raise
            finally:
                client.close()
