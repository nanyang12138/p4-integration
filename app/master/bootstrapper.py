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
import random
import socket

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
        self.remote_python = ssh_config.get("python_path", "python3")
        
    def deploy_agent(self, master_host: str, master_port: int, workspace: str, agent_server) -> tuple[str, str]:
        """
        Deploy Agent via SSH or Locally.
        Returns (agent_pid, agent_hint) if successful.
        """
        # 1. Read Agent Source Code
        current_dir = os.path.dirname(os.path.abspath(__file__))
        agent_path = os.path.join(current_dir, "../agent/agent_core.py")
        
        # Check for Local Mode
        hostname = socket.gethostname()
        # Resolve localhost/127.0.0.1 to verify if ssh_host points to this machine
        is_local = self.ssh_host in ["localhost", "127.0.0.1", "0.0.0.0", hostname]
        
        # Also check if ssh_host resolves to a local IP
        if not is_local:
            try:
                target_ip = socket.gethostbyname(self.ssh_host)
                local_ip = socket.gethostbyname(hostname)
                if target_ip == "127.0.0.1" or target_ip == local_ip:
                    is_local = True
            except:
                pass
        
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
                
                # Log to file for debugging
                log_path = f"/tmp/p4_agent_local_{int(time.time())}.log"
                try:
                    log_file = open(log_path, "w")
                    logger.info(f"Agent logging to {log_path}")
                except:
                    log_file = subprocess.DEVNULL

                popen_kwargs = {
                    'stdout': log_file,
                    'stderr': subprocess.STDOUT,
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

            # Execute via SSH
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            try:
                # SSH connection with retry mechanism
                logger.info(f"Connecting to {self.ssh_user}@{self.ssh_host}:{self.ssh_port}...")
                
                max_retries = 3
                last_error = None
                
                for attempt in range(max_retries):
                    try:
                        # Connect with password and longer timeout
                        client.connect(
                            hostname=self.ssh_host,
                            username=self.ssh_user,
                            password=self.ssh_password,
                            port=self.ssh_port,
                            timeout=30,  # Longer timeout
                            banner_timeout=30,
                            auth_timeout=30
                        )
                        logger.info("SSH connection successful!")
                        break  # Success, exit retry loop
                    except Exception as e:
                        last_error = e
                        if attempt < max_retries - 1:
                            wait_time = 2 ** (attempt + 1)  # 2s, 4s, 8s
                            logger.warning(f"SSH attempt {attempt + 1}/{max_retries} failed: {e}. Retrying in {wait_time}s...")
                            time.sleep(wait_time)
                        else:
                            error_msg = f"SSH connection failed after {max_retries} attempts for {self.ssh_user}@{self.ssh_host}\n"
                            error_msg += f"Last error: {last_error}\n"
                            raise RuntimeError(error_msg) from last_error

                # Use SFTP to upload the file directly (Reliable!)
                sftp = client.open_sftp()
                remote_agent_path = f"/tmp/p4_agent_{int(time.time())}_{random.randint(1000, 9999)}.py"
                logger.info(f"Uploading agent to {remote_agent_path}")
                
                # Write content directly to remote file
                with sftp.file(remote_agent_path, "w") as f:
                    f.write(agent_code)
                
                sftp.close()
                
                # Make executable (optional, but good practice)
                client.exec_command(f"chmod +x {remote_agent_path}")
                
                # Start Agent
                # Explicitly set log file via shell redirect to ensure it exists even if python fails early
                # The agent script *also* logs to a file, but this catches startup errors (like import errors)
                log_file_path = f"/tmp/p4_agent_boot_{int(time.time())}.log"
                
                remote_cmd = (
                    f"sh -c 'nohup {self.remote_python} {remote_agent_path} {master_host} {master_port} {workspace} "
                    f"> {log_file_path} 2>&1 & echo $!'"
                )
                
                logger.info(f"Starting agent: {remote_cmd}")
                stdin, stdout, stderr = client.exec_command(remote_cmd)
                
                pid_output = stdout.read().decode().strip()
                err_output = stderr.read().decode().strip()
                
                logger.info(f"Remote stdout: {pid_output}")
                if err_output:
                    logger.warning(f"Remote stderr: {err_output}")
                
                # Log where to find the logs
                logger.info(f"Agent startup logs: {log_file_path}")
                
                if pid_output and pid_output.isdigit():
                    pid = pid_output
                    logger.info(f"SSH Agent started with PID {pid}")
                    # We do NOT delete the file immediately, let it run. 
                    # Cleanup could be handled by the agent itself or a cron, 
                    # but for now leaving it is safer for debugging.
                    return pid, agent_hint
                else:
                     raise RuntimeError(f"Failed to get PID. Output: {pid_output}. Error: {err_output}")
                    
            except Exception as e:
                logger.error(f"SSH deployment failed: {e}")
                raise
            finally:
                client.close()
