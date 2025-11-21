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

# Windows constants
if sys.platform == 'win32':
    CREATE_NO_WINDOW = 0x08000000
else:
    CREATE_NO_WINDOW = 0

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
                
                # Start process detached/background
                # Different handling for Windows vs Unix
                popen_kwargs = {
                    'stdout': subprocess.DEVNULL,
                    'stderr': subprocess.DEVNULL,
                }
                
                if sys.platform == 'win32':
                    # Windows: Use CREATE_NO_WINDOW to run in background
                    popen_kwargs['creationflags'] = CREATE_NO_WINDOW
                else:
                    # Unix: Use start_new_session
                    popen_kwargs['start_new_session'] = True
                
                process = subprocess.Popen(cmd, **popen_kwargs)
                
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
            # Force bash execution to avoid csh/tcsh issues
            # Use double quotes for the outer command to avoid quote nesting issues
            remote_cmd = (
                f'/bin/bash << "BASH_SCRIPT_EOF"\n'
                f'TEMP_AGENT=/tmp/p4_agent_$$.py\n'
                f'cat > $TEMP_AGENT << AGENT_EOF\n'
                f'import base64\n'
                f'import sys\n'
                f'agent_code = base64.b64decode("{agent_code_b64}")\n'
                f'exec(agent_code)\n'
                f'AGENT_EOF\n'
                f'python3 $TEMP_AGENT {master_host} {master_port} {workspace} > /tmp/agent_$$.log 2>&1 &\n'
                f'AGENT_PID=$!\n'
                f'rm -f $TEMP_AGENT\n'
                f'echo $AGENT_PID\n'
                f'BASH_SCRIPT_EOF\n'
            )
            
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
                
                # Log the command for debugging
                logger.info(f"Executing remote command on {self.ssh_host}")
                logger.debug(f"Command: {remote_cmd[:200]}...")  # Log first 200 chars
                
                stdin, stdout, stderr = client.exec_command(remote_cmd)
                
                # Read output
                pid_output = stdout.read().decode().strip()
                err_output = stderr.read().decode().strip()
                
                # Log full output for debugging
                logger.info(f"Remote stdout: {pid_output}")
                if err_output:
                    logger.warning(f"Remote stderr: {err_output}")
                
                # Extract PID (last line of output)
                if pid_output:
                    lines = pid_output.strip().split('\n')
                    pid = lines[-1].strip()  # Last line should be the PID
                else:
                    pid = None
                
                if not pid or not pid.isdigit():
                    error_details = f"Agent start failed.\n"
                    error_details += f"Expected PID but got: {repr(pid_output)}\n"
                    if err_output:
                        error_details += f"Stderr: {err_output}\n"
                    error_details += f"\nDebug: Check /tmp/agent_*.log on {self.ssh_host}"
                    logger.error(error_details)
                    raise RuntimeError(error_details)
                    
                logger.info(f"SSH Agent started with PID {pid} on {self.ssh_host}")
                return pid, agent_hint
                
            except Exception as e:
                logger.error(f"SSH deployment failed: {e}")
                raise
            finally:
                client.close()
