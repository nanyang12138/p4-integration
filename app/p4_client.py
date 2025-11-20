import os
import logging
from typing import List, Tuple, Dict, Optional

from .runner import BaseRunner, LocalRunner, SSHRunner
import time


logger = logging.getLogger(__name__)


class P4Error(Exception):
    pass


class P4Client:
    def __init__(self, config: Dict[str, object]):
        self._config = config
        p4cfg = config.get("p4", {})
        assert isinstance(p4cfg, dict)
        self.port = str(p4cfg.get("port", ""))
        self.user = str(p4cfg.get("user", ""))
        self.client = str(p4cfg.get("client", ""))
        self.password = str(p4cfg.get("password", ""))
        self.p4_bin = str(p4cfg.get("bin", "p4"))

        exec_cfg = config.get("exec", {})
        self.exec_mode = "local"
        self.workspace_root = os.path.abspath(str(config.get("workspace_root", "ws")))
        self._ssh_prelude: str = ""
        self._ssh_shell: str = "/bin/sh"
        self._ssh_login_shell: bool = False
        if isinstance(exec_cfg, dict) and exec_cfg.get("mode") == "ssh":
            self.exec_mode = "ssh"
            ssh_cfg = exec_cfg.get("ssh", {})
            assert isinstance(ssh_cfg, dict)
            self._ssh_prelude = str(ssh_cfg.get("env_prelude", ""))
            self._ssh_shell = str(ssh_cfg.get("shell", "/bin/bash")) or "/bin/bash"
            self._ssh_login_shell = bool(ssh_cfg.get("login_shell", True))
            ssh_host = str(ssh_cfg.get("host"))
            ssh_user = str(ssh_cfg.get("user"))
            ssh_port = int(ssh_cfg.get("port", 22))
            ssh_key = str(ssh_cfg.get("key_path")) if ssh_cfg.get("key_path") else None
            # Expand ~ in key path
            if ssh_key:
                original_key = ssh_key
                if ssh_key.startswith("~"):
                    ssh_key = os.path.expanduser(ssh_key)
                logger.debug(f"SSH key path: original='{original_key}' -> expanded='{ssh_key}'")
                logger.debug(f"Key file exists: {os.path.exists(ssh_key) if ssh_key else 'N/A'}")
                if ssh_key and os.path.exists(ssh_key):
                    import stat
                    st = os.stat(ssh_key)
                    logger.debug(f"Key file permissions: {oct(st.st_mode)}")
            ssh_password = str(ssh_cfg.get("password")) if ssh_cfg.get("password") else None
            logger.debug(f"SSH config: host={ssh_host}, user={ssh_user}, port={ssh_port}, key_path={ssh_key}, has_password={bool(ssh_password)}")
            self.runner: BaseRunner = SSHRunner(
                host=ssh_host,
                user=ssh_user,
                port=ssh_port,
                key_path=ssh_key,
                password=ssh_password,
                shell_path=self._ssh_shell,
                login_shell=self._ssh_login_shell,
            )
            self.workspace_root = str(exec_cfg.get("workspace_root", self.workspace_root))
            # Default prelude to ensure env for all P4 commands
            if not self._ssh_prelude:
                self._ssh_prelude = (
                    f"cd {self.workspace_root}; "
                    "source /proj/verif_release_ro/cbwa_initscript/current/cbwa_init.bash; "
                    "bootenv"
                )
            # Optional override for p4 binary in SSH mode via env
            remote_p4_bin = os.environ.get("P4_INTEG_P4_BIN_SSH")
            if remote_p4_bin:
                self.p4_bin = remote_p4_bin
        else:
            self.runner = LocalRunner()
            try:
                if not os.path.isdir(self.workspace_root):
                    os.makedirs(self.workspace_root, exist_ok=True)
            except PermissionError:
                # Unable to create workspace_root (likely outside writable area); assume it exists/accessible
                pass

        # Derive P4CLIENT from a P4CONFIG file located directly under workspace_root (non-recursive)
        # Applies to both local and ssh modes. If found, overrides configured client.
        logger.debug(f"P4Client init: workspace_root={self.workspace_root}, exec_mode={self.exec_mode}, current_client={self.client}")
        derived = self._derive_client_from_p4config()
        logger.debug(f"P4CONFIG derived client: {derived}")
        if derived:
            logger.info(f"Overriding client from '{self.client}' to '{derived}'")
            self.client = derived
        else:
            # Fallback to config.yaml client if P4CONFIG not found
            logger.debug(f"P4CONFIG not found, using client from config: '{self.client}'")
            if not self.client:
                raise P4Error("P4CLIENT not configured in config.yaml and P4CONFIG not found in workspace_root")

    def _env(self) -> Dict[str, str]:
        env = {}
        if self.port:
            env["P4PORT"] = self.port
        if self.user:
            env["P4USER"] = self.user
        if self.client:
            env["P4CLIENT"] = self.client
        return env

    def _inline_env(self) -> str:
        import shlex
        env = self._env()
        if not env:
            return ""
        return " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items())

    def _run(self, args: List[str], input_text: Optional[str] = None) -> Tuple[int, str, str]:
        # Treat any integrate using branch spec (-b <name>) as special: no -c injection; keep prelude on SSH
        def _is_branch_integrate(a: List[str]) -> bool:
            try:
                return bool(a) and a[0] == "integrate" and "-b" in a
            except Exception:
                return False
        is_branch = _is_branch_integrate(args)
        if self.exec_mode == "ssh":
            import shlex
            inline = self._inline_env()
            
            # Determine the actual p4 command (binary + arguments)
            cmd_args: List[str] = [self.p4_bin]
            if not is_branch and self.client:
                # For non-branch commands, explicitly set the client context
                cmd_args += ["-c", self.client]
            cmd_args += args
            
            base_cmd = " ".join(shlex.quote(a) for a in cmd_args)
            
            # If inline envs are needed, prepend them
            if inline:
                base_cmd = f"{inline} {base_cmd}"
            
            ws_path = shlex.quote(self.workspace_root)
            
            # Build the compound shell command
            script_parts = []
            
            # 1. Ensure we are in the workspace root
            script_parts.append(f"cd {ws_path}")
            
            # 2. Run prelude if it exists (silently)
            if self._ssh_prelude:
                script_parts.append(f"({self._ssh_prelude}) >/dev/null 2>&1 || true")
            
            # 3. Run the actual p4 command
            script_parts.append(base_cmd)
            
            full_script = " && ".join(script_parts)
            
            # Execute via run_script which wraps in the shell
            return self.runner.run_script(full_script, shell_path=self._ssh_shell, env=self._env(), input_text=input_text)

        # Local path
        if is_branch:
            # Do not inject -c for branch integrate
            return self.runner.run([self.p4_bin] + args, cwd=self.workspace_root, env=self._env(), input_text=input_text)
        full: List[str] = [self.p4_bin]
        if self.client:
            full += ["-c", self.client]
        return self.runner.run(full + args, cwd=self.workspace_root, env=self._env(), input_text=input_text)

    def _run_with_retry(self, args: List[str], input_text: Optional[str] = None) -> Tuple[int, str, str]:
        retry_cfg = (self._config.get("p4", {}) if isinstance(self._config.get("p4", {}), dict) else {})
        max_attempts = int(retry_cfg.get("retries", 2))
        backoff_ms = int(retry_cfg.get("retry_backoff_ms", 300))
        attempt = 0
        last: Tuple[int, str, str] = (0, "", "")
        while attempt < max_attempts:
            attempt += 1
            code, out, err = self._run(args, input_text=input_text)
            last = (code, out, err)
            if code == 0:
                return last
            text = (out or "") + ("\n" + err if err else "")
            transient = any(tok in text.lower() for tok in ["timed out", "timeout", "connection", "eof", "temporar", "reset by peer"])  # noqa: E501
            if attempt < max_attempts and transient:
                time.sleep(backoff_ms / 1000.0)
                continue
            break
        return last

    def _derive_client_from_p4config(self) -> Optional[str]:
        """Strictly read P4CLIENT from 'P4CONFIG' under workspace_root (non-recursive).
        Also reads P4PORT if present. Returns client name if found, else None.
        """
        filename = "P4CONFIG"

        def _parse_lines(lines: List[str]) -> Dict[str, str]:
            vals: Dict[str, str] = {}
            for raw in lines:
                line = (raw or "").strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip().upper()
                val = val.strip().strip('"')
                if key in ("P4CLIENT", "P4PORT"):
                    vals[key] = val
            return vals

        # Local mode
        if self.exec_mode != "ssh":
            path = os.path.join(self.workspace_root, filename)
            if not os.path.isfile(path):
                logger.debug(f"Strict mode: {filename} not found at {path}")
                return None
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    vals = _parse_lines(f.readlines())
                if vals.get("P4PORT"):
                    logger.info(f"Updating P4PORT from '{self.port}' to '{vals['P4PORT']}' (from {filename})")
                    self.port = vals["P4PORT"]
                return vals.get("P4CLIENT")
            except Exception:
                return None

        # SSH mode
        try:
            code, _out, _err = self.runner.run(["test", "-f", filename], cwd=self.workspace_root)
            if code != 0:
                logger.debug(f"Strict mode: {filename} not found at {self.workspace_root}/{filename}")
                return None
            code, out, err = self.runner.run(["cat", filename], cwd=self.workspace_root)
            if code != 0:
                logger.debug(f"Failed to read {filename}: code={code}, err='{err}'")
                return None
            lines = (out or "").splitlines()
            vals = _parse_lines(lines)
            if vals.get("P4PORT"):
                logger.info(f"Found P4PORT: '{vals['P4PORT']}', updating from '{self.port}'")
                self.port = vals["P4PORT"]
            return vals.get("P4CLIENT")
        except Exception:
            return None

    def login(self) -> None:
        if not self.password:
            # No password configured; rely on existing ticket/environment
            return
        code, out, err = self._run_with_retry(["login", "-s"])
        if code == 0:
            return
        try:
            self._run_with_retry(["trust", "-f", "-y"])  # ignore result
        except Exception:  # noqa: BLE001
            pass
        code, out, err = self._run_with_retry(["login"], input_text=self.password + "\n")
        if code != 0:
            raise P4Error(f"p4 login failed: {err or out}")

    def create_changelist(self, description: str) -> int:
        # Build spec using server template to avoid missing/extra fields.
        # Then replace only the Description block, ensuring each line is tab-indented.
        tmpl_code, tmpl_out, tmpl_err = self._run_with_retry(["change", "-o"])
        if tmpl_code != 0 or not tmpl_out:
            raise P4Error(f"p4 change -o failed: {tmpl_err or tmpl_out}")

        # Ensure every line starts with a tab (Perforce requirement)
        lines = (description or "").splitlines()
        if not lines:
            lines = [""]
        desc_lines = [ln if ln.startswith("\t") else "\t" + ln for ln in lines]

        # Replace Description: block in the template with our tab-indented lines
        tmpl_lines = tmpl_out.splitlines()
        spec_lines: List[str] = []
        i = 0
        while i < len(tmpl_lines):
            line = tmpl_lines[i]
            spec_lines.append(line)
            if line.strip() == "Description:":
                # Inject our description lines
                spec_lines.extend(desc_lines)
                # Skip any existing tab-indented description lines in the template
                i += 1
                while i < len(tmpl_lines) and tmpl_lines[i].startswith("\t"):
                    i += 1
                # Continue without the normal i += 1 (we already advanced)
                continue
            i += 1

        spec = "\n".join(spec_lines) + "\n"
        code, out, err = self._run_with_retry(["change", "-i"], input_text=spec)
        if code != 0:
            raise P4Error(f"p4 change -i failed: {err or out}")
        for token in out.split():
            if token.isdigit():
                return int(token)
        raise P4Error(f"Unable to parse changelist from output: {out}")

    def reopen_to_changelist(self, changelist: int, files: List[str]) -> None:
        if not files:
            return
        # Reopen files in batches to avoid command line too long
        batch: List[str] = []
        for f in files:
            if f:
                batch.append(f)
            if len(batch) >= 50:
                code, out, err = self._run_with_retry(["reopen", "-c", str(changelist)] + batch)
                if code != 0:
                    raise P4Error(f"p4 reopen failed: {err or out}")
                batch = []
        if batch:
            code, out, err = self._run_with_retry(["reopen", "-c", str(changelist)] + batch)
            if code != 0:
                raise P4Error(f"p4 reopen failed: {err or out}")

    def shelve_changelist(self, changelist: int) -> Tuple[str, str]:
        code, out, err = self._run_with_retry(["shelve", "-r", "-c", str(changelist)])
        if code != 0 and "no files to shelve" not in (out.lower() + err.lower()):
            raise P4Error(f"p4 shelve failed: {err or out}")
        return out, err

    def shelve_first(self, changelist: int) -> Tuple[str, str]:
        code, out, err = self._run_with_retry(["shelve", "-f", "-c", str(changelist)])
        if code != 0 and "no files to shelve" not in (out.lower() + err.lower()):
            raise P4Error(f"p4 shelve failed: {err or out}")
        return out, err

    def p4push(self, changelist: int, trial: bool = False) -> Tuple[str, str]:
        # Ensure workspace env init before p4push, like other p4 commands
        from .env_helper import EnvInitHelper
        
        flags: List[str] = []
        if trial:
            flags.append("-trial")
        import shlex
        ws = shlex.quote(self.workspace_root)
        flag_str = " ".join(flags)
        inline = self._inline_env()
        envp = (inline + " ") if inline else ""
        
        # Use centralized env init helper
        env_helper = EnvInitHelper(self._config)
        init_cmds = env_helper.get_init_commands()
        
        script = (
            f"cd {ws}; "
            f"{init_cmds}"
            f"{envp}p4push {flag_str} -c {int(changelist)}"
        ).strip()
        code, out, err = self.runner.run_script(script, shell_path="/bin/bash", cwd=self.workspace_root, env=self._env())
        if code != 0:
            raise P4Error(f"p4push failed: {err or out}")
        return out, err

    def revert_files(self, files: List[str]) -> Tuple[str, str]:
        if not files:
            return "", ""
        log: List[str] = []
        batch: List[str] = []
        for f in files:
            if f:
                batch.append(f)
            if len(batch) >= 50:
                code, out, err = self._run_with_retry(["revert"] + batch)
                log.append((out or "") + ("\n" + err if err else ""))
                batch = []
        if batch:
            code, out, err = self._run_with_retry(["revert"] + batch)
            log.append((out or "") + ("\n" + err if err else ""))
        return "\n".join(log), ""

    def shelve_delete(self, changelist: int) -> Tuple[str, str]:
        code, out, err = self._run_with_retry(["shelve", "-d", "-c", str(changelist)])
        return out or "", err or ""

    def change_delete(self, changelist: int) -> Tuple[str, str]:
        code, out, err = self._run_with_retry(["change", "-d", str(changelist)])
        return out or "", err or ""

    def shelf_exists(self, changelist: int) -> bool:
        # Returns True if changelist has a shelf; false otherwise
        code, out, err = self._run_with_retry(["describe", "-S", "-s", str(changelist)])
        return code == 0

    # Workspace file I/O (text)
    def read_file_text(self, abs_path: str) -> str:
        code, out, err = self.runner.run(["cat", abs_path], cwd=self.workspace_root)
        if code != 0:
            raise P4Error(f"read file failed: {err or out}")
        return out

    def write_file_text(self, abs_path: str, content: str) -> None:
        # Robust write that works over SSH without fragile shell quoting
        if self.exec_mode == "ssh":
            import base64
            # Encode content to base64 and stream via stdin to base64 -d to avoid heredoc/quoting issues
            b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
            path_escaped = abs_path.replace('"', '\\"')
            # Ensure directory exists and write file by decoding stdin
            script = f'mkdir -p "$(dirname \\"{path_escaped}\\")" && base64 -d > "{path_escaped}"'
            code, out, err = self.runner.run_script(
                script,
                shell_path="/bin/bash",
                cwd=self.workspace_root,
                env=self._env(),
                input_text=b64 + "\n",
            )
            if code != 0:
                raise P4Error(f"write file failed: {err or out}")
            return
        # Local path: here-doc is safe locally (no remote shell -c quoting)
        script = f"cat > {abs_path} <<'EOF'\n{content}\nEOF"
        code, out, err = self.runner.run_script(script, shell_path="/bin/bash", cwd=self.workspace_root)
        if code != 0:
            raise P4Error(f"write file failed: {err or out}")

    # Default changelist (no explicit -c), operations
    def resolve_preview(self, changelist: int) -> Tuple[List[str], str]:
        # For compatibility, ignore changelist and preview across client default context
        code, out, err = self._run_with_retry(["resolve", "-n"])  # no -c
        if code != 0 and not out:
            raise P4Error(f"p4 resolve -n failed: {err or out}")
        files: List[str] = []
        for line in out.splitlines():
            s = line.strip()
            if not s:
                continue
            # Only count true merge-needed lines
            if " - merging " not in s:
                continue
            path = s.split(" - ", 1)[0].strip()
            if path:
                files.append(path)
        return files, (out or err or "")

    def resolve_preview_default(self) -> Tuple[List[str], str]:
        # Ensure we run inside the configured client/workspace
        # Also normalize file paths to absolute workspace paths
        code, out, err = self._run_with_retry(["resolve", "-n"])  # no -c
        if code != 0 and not out:
            raise P4Error(f"p4 resolve -n failed: {err or out}")
        files: List[str] = []
        ws = self.workspace_root.rstrip("/")
        for line in out.splitlines():
            s = line.strip()
            if not s:
                continue
            if " - merging " not in s:
                continue
            token = s.split(" - ", 1)[0].strip()
            if not token:
                continue
            # p4 may output client-rooted absolute paths already; keep as-is
            if token.startswith(ws + "/") or token.startswith("/"):
                files.append(token)
            else:
                # Make it absolute under workspace
                files.append(ws + "/" + token)
        return files, (out or err or "")

    def resolve_preview_shell(self) -> Tuple[List[str], str]:
        """Run resolve -n using explicit client with -c, without sourcing env."""
        if self.exec_mode != "ssh":
            return self.resolve_preview_default()
        code, out, err = self._run_with_retry(["resolve", "-n"])  # _run injects -c
        text = (out or "") + (("\n" + err) if err else "")
        files: List[str] = []
        ws_prefix = self.workspace_root.rstrip("/") + "/"
        for line in text.splitlines():
            s = line.strip()
            if not s or " - merging " not in s:
                continue
            tok = s.split(" - ", 1)[0].strip()
            if tok.startswith("/"):
                files.append(tok)
            else:
                files.append(ws_prefix + tok)
        return files, text

    def opened_in_default(self) -> Tuple[List[str], str]:
        code, out, err = self._run_with_retry(["opened"])  # no -c
        if code != 0:
            raise P4Error(f"p4 opened failed: {err or out}")
        files: List[str] = []
        for line in out.splitlines():
            tok = line.strip().split("#")[0].strip()
            if tok:
                files.append(tok)
        return files, out

    def submit(self, changelist: int) -> Tuple[str, str]:
        code, out, err = self._run_with_retry(["submit", "-c", str(changelist)])
        if code != 0:
            raise P4Error(f"p4 submit failed: {err or out}")
        return out, err

    def submit_default(self, description: str) -> Tuple[str, str]:
        code, out, err = self._run_with_retry(["submit", "-d", description])
        if code != 0:
            raise P4Error(f"p4 submit failed: {err or out}")
        return out, err

    # Preview merged content to stdout (does not modify files)
    def preview_merge(self, file_path: str, changelist: Optional[int] = None, mode: str = "merge") -> str:
        flag = {
            "accept-theirs": "-at",
            "accept-yours": "-ay",
            "merge": "-am",
        }.get(mode, "-am")
        # Preview current client default context (no -c)
        args: List[str] = ["resolve", "-o", flag]
        args.append(file_path)
        code, out, err = self._run_with_retry(args)
        if code != 0 and not out:
            raise P4Error(f"p4 resolve -o failed: {err or out}")
        return out or err or ""

    def preview_merge_bundle(self, file_path: str) -> Dict[str, str]:
        """Return all three previews (merge/theirs/yours) in one call to reduce latency."""
        if self.exec_mode == "ssh":
            import shlex
            f = shlex.quote(file_path)
            p4 = shlex.quote(self.p4_bin)
            inline = self._inline_env()
            envp = (inline + " ") if inline else ""
            client_opt = ("-c " + shlex.quote(self.client)) if self.client else ""
            script = (
                f"echo '===MERGE==='; {envp}{p4} {client_opt} resolve -o -am {f} 2>&1; "
                f"echo '===THEIRS==='; {envp}{p4} {client_opt} resolve -o -at {f} 2>&1; "
                f"echo '===YOURS==='; {envp}{p4} {client_opt} resolve -o -ay {f} 2>&1; "
                f"echo '===END==='"
            )
            code, out, err = self.runner.run_script(script, shell_path=self._ssh_shell, cwd=self.workspace_root, env=self._env())
            if code != 0 and not out:
                raise P4Error(f"preview bundle failed: {err or out}")
            text = out or err or ""
            def _extract(section: str) -> str:
                import re
                pat = re.compile(rf"===\Q{section}\E===\n([\s\S]*?)(?===|\Z)")
                m = pat.search(text)
                return m.group(1) if m else ""
            return {
                "merge": _extract("MERGE").strip("\n"),
                "theirs": _extract("THEIRS").strip("\n"),
                "yours": _extract("YOURS").strip("\n"),
            }
        # local: just call three times
        return {
            "merge": self.preview_merge(file_path, None, "merge"),
            "theirs": self.preview_merge(file_path, None, "accept-theirs"),
            "yours": self.preview_merge(file_path, None, "accept-yours"),
        }

    # Streaming helpers
    def run_p4_stream(self, args: List[str], on_chunk) -> int:
        # Treat any branch-spec integrate (-b <name>) as special
        def _is_branch_integrate() -> bool:
            try:
                return len(args) >= 2 and args[0] == "integrate" and "-b" in args
            except Exception:
                return False
        
        def _is_resolve() -> bool:
            try:
                return len(args) >= 1 and args[0] == "resolve"
            except Exception:
                return False

        special = _is_branch_integrate()  # Only branch integrate needs special handling
        resolve_cmd = _is_resolve()
        if self.exec_mode == "ssh":
            import shlex
            inline = self._inline_env()
            if special or resolve_cmd:
                # p4 commands need cd but don't need full env prelude (cbwa_init/bootenv)
                import shlex
                ws = shlex.quote(self.workspace_root)
                if special:
                    # Branch integrate commands don't use -c client
                    base_cmd = " ".join(shlex.quote(a) for a in [self.p4_bin] + args)
                else:
                    # Resolve commands use -c client
                    base_cmd = " ".join(shlex.quote(a) for a in [self.p4_bin] + args)
                    if self.client:
                        base_cmd = " ".join(shlex.quote(a) for a in [self.p4_bin, "-c", self.client] + args)
                cmd = f"env {inline} {base_cmd}" if inline else base_cmd
                # Use background process to get actual p4 PID, with cd but no env init
                script = f"cd {ws}; {cmd} & echo PID:$! ; wait"
                return self.runner.run_script_stream(script, shell_path=self._ssh_shell, cwd=self.workspace_root, env=self._env(), input_text=None, on_chunk=on_chunk)  # type: ignore[attr-defined]
            # Default: inject -c <client> and skip sourcing
            parts: List[str] = [self.p4_bin]
            if self.client:
                parts += ["-c", self.client]
            parts += args
            base_cmd = " ".join(shlex.quote(a) for a in parts)
            cmd = f"env {inline} {base_cmd}" if inline else base_cmd
            script = f"echo PID:$$; exec {cmd}"
            return self.runner.run_script_stream(script, shell_path=self._ssh_shell, cwd=self.workspace_root, env=self._env(), input_text=None, on_chunk=on_chunk)  # type: ignore[attr-defined]
        # local
        import shlex
        ws = shlex.quote(self.workspace_root)
        if special or resolve_cmd:
            # p4 commands need cd but don't need full env prelude
            if special:
                # Branch integrate commands don't use -c client
                parts_local = [self.p4_bin] + args
            else:
                # Resolve commands use -c client
                parts_local = [self.p4_bin]
                if self.client:
                    parts_local += ["-c", self.client]
                parts_local += args
            cmd = " ".join(shlex.quote(a) for a in parts_local)
            # Use background process to get actual p4 PID, with cd
            script = f"cd {ws}; {cmd} & echo PID:$! ; wait"
        else:
            # Other commands use the default exec approach
            parts_local = [self.p4_bin]
            if self.client:
                parts_local += ["-c", self.client]
            parts_local += args
            cmd = " ".join(shlex.quote(a) for a in parts_local)
            script = f"echo PID:$$; exec {cmd}"
        return self.runner.run_script_stream(script, shell_path="/bin/bash", cwd=self.workspace_root, env=self._env(), input_text=None, on_chunk=on_chunk)  # type: ignore[attr-defined]

    def p4push_stream(self, changelist: int, trial: bool, on_chunk) -> int:
        from .env_helper import EnvInitHelper
        
        flags: List[str] = []
        if trial:
            flags.append("-trial")
        import shlex
        ws = shlex.quote(self.workspace_root)
        flag_str = " ".join(flags)
        inline = self._inline_env()
        envp = (inline + " ") if inline else ""
        
        # Use centralized env init helper
        env_helper = EnvInitHelper(self._config)
        init_cmds = env_helper.get_init_commands()
        
        script = (
            f"cd {ws}; "
            f"{init_cmds}"
            "echo PID:$$; "
            f"{envp}p4push {flag_str} -c {int(changelist)}"
        ).strip()
        # Some environments prompt during p4push (e.g., p4w), so feed password if configured
        try:
            pw = str(self._config.get("p4w_password") or "")
        except Exception:
            pw = ""
        input_text = (pw + "\n") if pw else None
        return self.runner.run_script_stream(
            script,
            shell_path="/bin/bash",
            cwd=self.workspace_root,
            env=self._env(),
            input_text=input_text,
            on_chunk=on_chunk,
        )  # type: ignore[attr-defined]

    def check_pid(self, pid: int) -> bool:
        try:
            if self.exec_mode == "ssh":
                script = f"( {self._ssh_prelude} ) >/dev/null 2>&1; kill -0 {int(pid)} >/dev/null 2>&1 && echo ALIVE || echo DEAD"
                code, out, err = self.runner.run_script(script, shell_path=self._ssh_shell, cwd=self.workspace_root)
                text = (out or "") + ("\n" + err if err else "")
                return "ALIVE" in text
            # local: use platform-appropriate check
            import os, platform
            if os.name == "nt" or platform.system() == "Windows":
                # Windows local: tasklist
                code, out, err = self.runner.run(["tasklist", "/FI", f"PID eq {int(pid)}"])  # type: ignore
                text = (out or "") + ("\n" + err if err else "")
                return str(int(pid)) in text
            else:
                # POSIX local (Linux/macOS): os.kill(pid, 0) to test existence
                try:
                    os.kill(int(pid), 0)
                    return True
                except ProcessLookupError:
                    return False
                except PermissionError:
                    # Process exists but we lack permission to signal it
                    return True
                except Exception:
                    return False
        except Exception:
            return False

    def kill_pid(self, pid: int, signal: int = 15) -> bool:
        """Kill a process by PID. Returns True if kill command was executed successfully."""
        try:
            if self.exec_mode == "ssh":
                # SSH: use kill command on remote host
                script = f"( {self._ssh_prelude} ) >/dev/null 2>&1; kill -{signal} {int(pid)} >/dev/null 2>&1 && echo KILLED || echo FAILED"
                code, out, err = self.runner.run_script(script, shell_path=self._ssh_shell, cwd=self.workspace_root)
                text = (out or "") + ("\n" + err if err else "")
                return "KILLED" in text
            # local: use platform-appropriate kill
            import os, platform, signal as sig
            if os.name == "nt" or platform.system() == "Windows":
                # Windows local: use taskkill
                code, out, err = self.runner.run(["taskkill", "/F", "/PID", str(int(pid))])  # type: ignore
                return code == 0
            else:
                # POSIX local (Linux/macOS): use os.kill
                try:
                    os.kill(int(pid), signal)
                    return True
                except ProcessLookupError:
                    # Process already dead
                    return True
                except PermissionError:
                    # No permission to kill, but try anyway with subprocess
                    import subprocess
                    try:
                        subprocess.run(["kill", f"-{signal}", str(int(pid))], check=True, capture_output=True)
                        return True
                    except Exception:
                        return False
                except Exception:
                    return False
        except Exception:
            return False
