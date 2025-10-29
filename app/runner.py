import os
import logging
import threading
import paramiko
import subprocess
from typing import Tuple, Optional, Dict, List


logger = logging.getLogger(__name__)


class CommandError(Exception):
    pass


class BaseRunner:
    def run(self, args: List[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, input_text: Optional[str] = None) -> Tuple[int, str, str]:
        raise NotImplementedError

    def run_script(self, script: str, shell_path: Optional[str] = None, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, input_text: Optional[str] = None, pty: bool = False) -> Tuple[int, str, str]:
        raise NotImplementedError

    # Streaming variants (optional override)
    def run_script_stream(self, script: str, shell_path: Optional[str], cwd: Optional[str], env: Optional[Dict[str, str]], input_text: Optional[str], on_chunk: Optional[callable]) -> int:
        # Default fallback: run normally and emit once
        code, out, err = self.run_script(script, shell_path=shell_path, cwd=cwd, env=env, input_text=input_text)
        if on_chunk:
            if out:
                on_chunk('stdout', out)
            if err:
                on_chunk('stderr', err)
        return code


class LocalRunner(BaseRunner):
    def run(self, args: List[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, input_text: Optional[str] = None) -> Tuple[int, str, str]:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            env=(dict(os.environ, **env) if env else None),
            stdin=subprocess.PIPE if input_text else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, err = proc.communicate(input=input_text)
        return proc.returncode, out, err

    def run_script(self, script: str, shell_path: Optional[str] = None, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, input_text: Optional[str] = None, pty: bool = False) -> Tuple[int, str, str]:
        shell = shell_path or "/bin/sh"
        args = [shell, "-c", script]
        return self.run(args, cwd=cwd, env=env, input_text=input_text)

    def run_script_stream(self, script: str, shell_path: Optional[str] = None, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, input_text: Optional[str] = None, on_chunk: Optional[callable] = None) -> int:
        shell = shell_path or "/bin/sh"
        args = [shell, "-c", script]
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            env=(dict(os.environ, **env) if env else None),
            stdin=subprocess.PIPE if input_text else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if input_text and proc.stdin:
            try:
                proc.stdin.write(input_text)
                proc.stdin.flush()
                proc.stdin.close()
            except Exception:
                pass
        def _reader(stream, tag):
            try:
                for line in iter(stream.readline, ''):
                    if on_chunk:
                        on_chunk(tag, line)
            finally:
                try:
                    stream.close()
                except Exception:
                    pass
        th_out = threading.Thread(target=_reader, args=(proc.stdout, 'stdout'), daemon=True)
        th_err = threading.Thread(target=_reader, args=(proc.stderr, 'stderr'), daemon=True)
        th_out.start(); th_err.start()
        code = proc.wait()
        th_out.join(timeout=0.1); th_err.join(timeout=0.1)
        return code


class SSHRunner(BaseRunner):
    def __init__(self, host: str, user: str, port: int = 22, key_path: Optional[str] = None, password: Optional[str] = None, shell_path: str = "/bin/sh", login_shell: bool = False):
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path
        self.password = password
        self.shell_path = shell_path or "/bin/sh"
        self.login_shell = login_shell
        self._client = None
        self._lock = threading.Lock()

    def _connect(self) -> paramiko.SSHClient:
        if self._client:
            try:
                tr = self._client.get_transport()
                if tr and tr.is_active():
                    return self._client
            except Exception:
                pass
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        # Authentication preference: explicit key -> password -> ssh-agent/default keys
        connected = False
        try:
            logger.debug(f"SSH connecting to {self.user}@{self.host}:{self.port}")
            
            # Try key authentication first if key_path is provided
            if self.key_path and not connected:
                logger.debug(f"Attempting key authentication: {self.key_path}")
                pkey = None
                try:
                    pkey = paramiko.RSAKey.from_private_key_file(self.key_path)
                    logger.debug("Successfully loaded RSA key")
                except Exception as e:
                    logger.debug(f"Failed to load RSA key: {e}")
                    try:
                        pkey = paramiko.Ed25519Key.from_private_key_file(self.key_path)  # type: ignore[attr-defined]
                        logger.debug("Successfully loaded Ed25519 key")
                    except Exception as e2:
                        logger.debug(f"Failed to load Ed25519 key: {e2}")
                        pkey = None
                
                if pkey:
                    try:
                        logger.debug("Connecting with private key...")
                        client.connect(
                            self.host,
                            port=self.port,
                            username=self.user,
                            pkey=pkey,
                            look_for_keys=False,
                            allow_agent=False,
                        )
                        logger.info("Key authentication successful")
                        connected = True
                    except Exception as e:
                        logger.debug(f"Key authentication failed: {e}")
                else:
                    logger.debug("No valid key loaded")
            
            # Try password authentication if we have a password and not yet connected
            if self.password and not connected:
                try:
                    logger.debug("Attempting password authentication")
                    client.connect(
                        self.host,
                        port=self.port,
                        username=self.user,
                        password=self.password,
                        look_for_keys=False,
                        allow_agent=False,
                    )
                    logger.info("Password authentication successful")
                    connected = True
                except Exception as e:
                    logger.debug(f"Password authentication failed: {e}")
            
            # Fall back to ssh-agent and default key files if not yet connected
            if not connected:
                logger.debug("Attempting default authentication (ssh-agent/keys)")
                client.connect(
                    self.host,
                    port=self.port,
                    username=self.user,
                    look_for_keys=True,
                    allow_agent=True,
                )
                logger.info("Default authentication successful")
                connected = True
        except Exception as e:
            logger.error(f"SSH connection failed: {e}")
            # Ensure client is closed on failure to avoid stale connections
            try:
                client.close()
            except Exception:
                pass
            raise
        try:
            tr = client.get_transport()
            if tr:
                tr.set_keepalive(30)
        except Exception:
            pass
        self._client = client
        return client

    def _env_prefix(self, env: Optional[Dict[str, str]]) -> str:
        if not env:
            return ""
        import shlex
        if "csh" in (self.shell_path or ""):
            lines = [f"setenv {k} {shlex.quote(str(v))}" for k, v in env.items()]
            return "; ".join(lines)
        assignments = " ".join([f"{k}={shlex.quote(str(v))}" for k, v in env.items()])
        return f"export {assignments}"

    def _shell_cmd(self, inner: str, shell_override: Optional[str] = None) -> str:
        import shlex
        shell = shell_override or self.shell_path
        flags: List[str] = []
        if self.login_shell:
            flags.append("-l")
        flags.append("-c")
        return f"{shell} {' '.join(flags)} {shlex.quote(inner)}"

    def run(self, args: List[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, input_text: Optional[str] = None) -> Tuple[int, str, str]:
        import shlex
        with self._lock:
            client = self._connect()
            segments: List[str] = []
            env_cmd = self._env_prefix(env)
            if env_cmd:
                segments.append(env_cmd)
            if cwd:
                segments.append(f"cd {shlex.quote(cwd)}")
            cmd = " ".join(shlex.quote(a) for a in args)
            segments.append(cmd)
            inner = " ; ".join(segments)
            full_cmd = self._shell_cmd(inner)
            stdin, stdout, stderr = client.exec_command(full_cmd)
            if input_text:
                stdin.write(input_text)
                stdin.flush()
                try:
                    stdin.channel.shutdown_write()
                except Exception:
                    pass
            exit_status = stdout.channel.recv_exit_status()
            out = stdout.read().decode()
            err = stderr.read().decode()
            return exit_status, out, err

    def run_script(self, script: str, shell_path: Optional[str] = None, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, input_text: Optional[str] = None, pty: bool = False) -> Tuple[int, str, str]:
        import shlex
        with self._lock:
            client = self._connect()
            segments: List[str] = []
            env_cmd = self._env_prefix(env)
            if env_cmd:
                segments.append(env_cmd)
            if cwd:
                segments.append(f"cd {shlex.quote(cwd)}")
            segments.append(script)
            inner = " ; ".join(segments)
            full_cmd = self._shell_cmd(inner, shell_override=shell_path)
            stdin, stdout, stderr = client.exec_command(full_cmd, get_pty=pty)
            if input_text:
                stdin.write(input_text)
                stdin.flush()
                try:
                    stdin.channel.shutdown_write()
                except Exception:
                    pass
            exit_status = stdout.channel.recv_exit_status()
            out = stdout.read().decode()
            err = stderr.read().decode()
            return exit_status, out, err

    def run_script_stream(self, script: str, shell_path: Optional[str] = None, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, input_text: Optional[str] = None, on_chunk: Optional[callable] = None) -> int:
        import shlex, time
        with self._lock:
            client = self._connect()
            segments: List[str] = []
            env_cmd = self._env_prefix(env)
            if env_cmd:
                segments.append(env_cmd)
            if cwd:
                segments.append(f"cd {shlex.quote(cwd)}")
            segments.append(script)
            inner = " ; ".join(segments)
            full_cmd = self._shell_cmd(inner, shell_override=shell_path)
            stdin, stdout, stderr = client.exec_command(full_cmd, get_pty=True)
            if input_text:
                try:
                    stdin.write(input_text)
                    stdin.flush()
                    stdin.channel.shutdown_write()
                except Exception:
                    pass
            ch_out = stdout.channel
            ch_err = stderr.channel
            while True:
                if ch_out.recv_ready():
                    try:
                        chunk = ch_out.recv(1024).decode(errors='ignore')
                        if chunk and on_chunk:
                            on_chunk('stdout', chunk)
                    except Exception:
                        pass
                if ch_err.recv_stderr_ready():
                    try:
                        chunk = ch_err.recv_stderr(1024).decode(errors='ignore')
                        if chunk and on_chunk:
                            on_chunk('stderr', chunk)
                    except Exception:
                        pass
                if stdout.channel.exit_status_ready():
                    break
                time.sleep(0.1)
            return stdout.channel.recv_exit_status()
