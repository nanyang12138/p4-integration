import os
import yaml
from typing import Any, Dict, Tuple, List


DEFAULT_CONFIG_PATH = os.environ.get("P4_INTEG_CONFIG", "config.yaml")


def load_config() -> Dict[str, Any]:
    config: Dict[str, Any] = {}
    cfg_path = os.environ.get("P4_INTEG_CONFIG", DEFAULT_CONFIG_PATH)
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            if isinstance(data, dict):
                config = data

    # Defaults
    p4 = config.setdefault("p4", {})
    p4.setdefault("port", os.environ.get("P4PORT", ""))
    p4.setdefault("user", os.environ.get("P4USER", ""))
    p4.setdefault("client", os.environ.get("P4CLIENT", ""))
    p4.setdefault("password", os.environ.get("P4PASSWD", ""))

    config.setdefault("workspace_root", os.environ.get("P4_INTEG_WORKSPACE_ROOT", os.path.abspath("ws")))
    config.setdefault("blocklist", [])  # list of glob patterns or exact depot paths
    config.setdefault("test_hook", {"command": "", "args": []})
    # Logging defaults (can be overridden by env LOG_LEVEL/LOG_FILE)
    logging_cfg = config.setdefault("logging", {})
    if isinstance(logging_cfg, dict):
        logging_cfg.setdefault("level", os.environ.get("LOG_LEVEL", "INFO"))
        logging_cfg.setdefault("file", os.environ.get("LOG_FILE", os.path.join("data", "p4_integ.log")))
        logging_cfg.setdefault("max_bytes", 2 * 1024 * 1024)
        logging_cfg.setdefault("backup_count", 5)
        logging_cfg.setdefault("json", False)

    # Exec/SSH env overrides
    exec_cfg = config.setdefault("exec", {}) if isinstance(config.get("exec", {}), dict) else {}
    mode_env = os.environ.get("P4_INTEG_EXEC_MODE")
    if mode_env:
        exec_cfg["mode"] = mode_env
    ws_root_env = os.environ.get("P4_INTEG_EXEC_WORKSPACE_ROOT")
    if ws_root_env:
        exec_cfg["workspace_root"] = ws_root_env
    ssh_cfg = exec_cfg.setdefault("ssh", {}) if isinstance(exec_cfg, dict) else {}
    host_env = os.environ.get("P4_INTEG_SSH_HOST")
    if host_env:
        ssh_cfg["host"] = host_env
        exec_cfg["mode"] = "ssh"
    user_env = os.environ.get("P4_INTEG_SSH_USER")
    if user_env:
        ssh_cfg["user"] = user_env
    port_env = os.environ.get("P4_INTEG_SSH_PORT")
    if port_env:
        try:
            ssh_cfg["port"] = int(port_env)
        except Exception:
            pass
    key_env = os.environ.get("P4_INTEG_SSH_KEY")
    if key_env:
        ssh_cfg["key_path"] = key_env
    pw_env = os.environ.get("P4_INTEG_SSH_PASSWORD")
    if pw_env:
        ssh_cfg["password"] = pw_env
    shell_env = os.environ.get("P4_INTEG_SSH_SHELL")
    if shell_env:
        ssh_cfg["shell"] = shell_env
    login_env = os.environ.get("P4_INTEG_SSH_LOGIN_SHELL")
    if login_env is not None:
        val = str(login_env).strip().lower()
        if val in ("1", "true", "yes", "on"):
            ssh_cfg["login_shell"] = True
        elif val in ("0", "false", "no", "off"):
            ssh_cfg["login_shell"] = False
    prelude_env = os.environ.get("P4_INTEG_SSH_ENV_PRELUDE")
    if prelude_env:
        ssh_cfg["env_prelude"] = prelude_env

    return config


def validate_config(config: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Validate and normalize configuration.

    Returns (config, warnings, errors).
    """
    warnings: List[str] = []
    errors: List[str] = []

    # Basic structure checks
    if not isinstance(config.get("p4", {}), dict):
        errors.append("p4 section must be a mapping")
        config["p4"] = {}

    p4 = config.get("p4", {})
    if isinstance(p4, dict):
        for key in ["port", "user", "client", "password", "bin"]:
            if key in p4 and p4[key] is not None and not isinstance(p4[key], str):
                try:
                    p4[key] = str(p4[key])
                except Exception:
                    warnings.append(f"p4.{key} coerced to string failed; clearing value")
                    p4[key] = ""

    # workspace_root
    wr = config.get("workspace_root")
    if not isinstance(wr, str) or not wr.strip():
        warnings.append("workspace_root not set; defaulting to ./ws")
        config["workspace_root"] = os.path.abspath("ws")
    else:
        # For local mode, require the workspace_root directory to exist
        exec_cfg_chk = config.get("exec", {}) if isinstance(config.get("exec", {}), dict) else {}
        is_ssh_mode = (exec_cfg_chk.get("mode") == "ssh")
        if not is_ssh_mode:
            try:
                if not os.path.isdir(config["workspace_root"]):
                    errors.append(f"workspace_root does not exist: {config['workspace_root']}")
            except Exception:
                # If any unexpected error occurs when checking, surface a generic error
                errors.append("workspace_root path check failed")

    # exec.ssh validation if present
    exec_cfg = config.get("exec")
    if exec_cfg and isinstance(exec_cfg, dict) and exec_cfg.get("mode") == "ssh":
        ssh = exec_cfg.get("ssh", {})
        if not isinstance(ssh, dict):
            errors.append("exec.ssh must be a mapping when exec.mode == 'ssh'")
        else:
            # Fill defaults and validate
            if not ssh.get("user"):
                default_user = os.environ.get("P4_INTEG_SSH_USER") or os.environ.get("USER") or os.environ.get("LOGNAME")
                if default_user:
                    ssh["user"] = default_user
                    warnings.append("exec.ssh.user not set; defaulting to current user")
            for req in ["host", "user"]:
                if not ssh.get(req):
                    errors.append(f"exec.ssh.{req} is required for ssh mode")
        # Require a workspace_root setting for ssh mode
        ws_ssh = exec_cfg.get("workspace_root")
        if not isinstance(ws_ssh, str) or not ws_ssh.strip():
            errors.append("exec.workspace_root is required when exec.mode == 'ssh'")

    # logging validation
    log_cfg = config.get("logging", {})
    if not isinstance(log_cfg, dict):
        warnings.append("logging section should be a mapping; using defaults")
        config["logging"] = {}
        load_config()  # re-apply defaults into section
    else:
        path = log_cfg.get("file")
        if not isinstance(path, str) or not path.strip():
            log_cfg["file"] = os.path.join("data", "p4_integ.log")
        try:
            mb = int(log_cfg.get("max_bytes", 0))
            if mb < 64 * 1024:
                warnings.append("logging.max_bytes too small; raising to 64KiB")
                log_cfg["max_bytes"] = 64 * 1024
        except Exception:
            log_cfg["max_bytes"] = 2 * 1024 * 1024

        try:
            bc = int(log_cfg.get("backup_count", 0))
            if bc < 1:
                log_cfg["backup_count"] = 3
        except Exception:
            log_cfg["backup_count"] = 5

    return config, warnings, errors
