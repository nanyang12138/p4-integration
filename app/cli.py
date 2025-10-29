import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from .config import load_config, validate_config
from .jobs import JobManager
from .p4_client import P4Client
from .logging_util import _setup_logging  # lightweight logging setup for CLI


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _ensure_manager(cfg: Dict[str, Any]) -> JobManager:
    # Create a standalone JobManager for CLI operations
    return JobManager(cfg)


def cmd_jobs_list(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    jm = _ensure_manager(cfg)
    jobs = jm.list_jobs()
    _print_json(jobs)
    return 0


def cmd_jobs_create(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    jm = _ensure_manager(cfg)
    payload: Dict[str, Any] = {
        "source": args.source or "",
        "target": args.target or "",
        "description": args.description or "Integration job",
    }
    if args.changelist:
        payload["changelist"] = int(args.changelist)
    if args.spec:
        payload["spec"] = args.spec
    job_id = jm.create_job(payload)
    _print_json({"id": job_id})
    return 0


def cmd_jobs_get(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    jm = _ensure_manager(cfg)
    job = jm.get_job(args.job_id)
    if not job:
        print(f"job not found: {args.job_id}", file=sys.stderr)
        return 2
    _print_json(job)
    return 0


def cmd_jobs_submit(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    jm = _ensure_manager(cfg)
    job = jm.admin_submit(args.job_id)
    _print_json(job)
    return 0


def cmd_jobs_cancel(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    jm = _ensure_manager(cfg)
    job = jm.admin_cancel(args.job_id)
    _print_json(job)
    return 0


def cmd_diag_health(args: argparse.Namespace, cfg: Dict[str, Any]) -> int:
    client = P4Client(cfg)
    info: Dict[str, Any] = {
        "exec_mode": getattr(client, "exec_mode", "local"),
        "client": client.client,
        "workspace_root": client.workspace_root,
        "port": client.port,
        "user": client.user,
    }
    try:
        if client.exec_mode == "local":
            exists = os.path.isdir(client.workspace_root)
            info["workspace_root_exists"] = exists
            code, out, err = client._run_with_retry(["-V"])  # use prelude-aware P4 invocation
            info["p4_present"] = (code == 0)
        else:
            code, out, err = client.runner.run(["test", "-d", client.workspace_root])  # type: ignore
            info["workspace_root_exists"] = (code == 0)
            code, out, err = client._run_with_retry(["-V"])  # use prelude-aware P4 invocation
            info["p4_present"] = (code == 0)
        info["ok"] = bool(info.get("workspace_root_exists")) and bool(info.get("p4_present"))
        _print_json(info)
        return 0 if info["ok"] else 1
    except Exception as e:
        info["ok"] = False
        info["error"] = str(e)
        _print_json(info)
        return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="p4-integ", description="P4 integration helper CLI")
    p.add_argument("--config", dest="config_path", default=None, help="Path to config.yaml (default: env P4_INTEG_CONFIG or ./config.yaml)")
    p.add_argument("--log-level", dest="log_level", default=None, help="Override log level (e.g. INFO, DEBUG)")
    # Execution/SSH overrides
    p.add_argument("--mode", dest="exec_mode", choices=["local", "ssh"], default=None, help="Override exec.mode (local or ssh)")
    p.add_argument("--ssh", dest="ssh_host", default=None, help="Quick set SSH host (implies --mode ssh)")
    p.add_argument("--ssh-user", dest="ssh_user", default=None, help="SSH username")
    p.add_argument("--ssh-port", dest="ssh_port", type=int, default=None, help="SSH port (default 22)")
    p.add_argument("--ssh-key", dest="ssh_key_path", default=None, help="SSH private key path")
    p.add_argument("--ssh-password", dest="ssh_password", default=None, help="SSH password (use env or key when possible)")
    p.add_argument("--ssh-shell", dest="ssh_shell", default=None, help="Remote shell path (default /bin/bash)")
    p.add_argument("--ssh-login-shell", dest="ssh_login_shell", action="store_true", help="Use login shell for SSH")
    p.add_argument("--no-ssh-login-shell", dest="ssh_login_shell", action="store_false", help="Do not use login shell for SSH")
    p.add_argument("--env-prelude", dest="ssh_env_prelude", default=None, help="Commands to prepare env on remote (sourced before running p4)")
    p.add_argument("--exec-workspace-root", dest="exec_workspace_root", default=None, help="Override exec.workspace_root")
    p.set_defaults(ssh_login_shell=None)

    sub = p.add_subparsers(dest="command", required=True)

    # jobs list
    sp = sub.add_parser("jobs", help="Manage jobs")
    sp_sub = sp.add_subparsers(dest="jobs_cmd", required=True)

    sp_list = sp_sub.add_parser("list", help="List jobs")
    sp_list.set_defaults(func=cmd_jobs_list)

    sp_create = sp_sub.add_parser("create", help="Create a new job")
    sp_create.add_argument("--source", required=False, help="Depot path spec for source")
    sp_create.add_argument("--target", required=False, help="Depot path spec for target")
    sp_create.add_argument("--description", required=False, help="Job description")
    sp_create.add_argument("--changelist", required=False, help="Changelist number (int)")
    sp_create.add_argument("--spec", required=False, help="Named spec from config.specs")
    sp_create.set_defaults(func=cmd_jobs_create)

    sp_get = sp_sub.add_parser("get", help="Get a job by id")
    sp_get.add_argument("job_id")
    sp_get.set_defaults(func=cmd_jobs_get)

    sp_submit = sp_sub.add_parser("submit", help="Submit a job's changelist")
    sp_submit.add_argument("job_id")
    sp_submit.set_defaults(func=cmd_jobs_submit)

    sp_cancel = sp_sub.add_parser("cancel", help="Cancel a job")
    sp_cancel.add_argument("job_id")
    sp_cancel.set_defaults(func=cmd_jobs_cancel)

    # diagnostics
    sp_diag = sub.add_parser("diag", help="Diagnostics")
    sp_diag_sub = sp_diag.add_subparsers(dest="diag_cmd", required=True)

    sp_health = sp_diag_sub.add_parser("health", help="Check P4/WS health")
    sp_health.set_defaults(func=cmd_diag_health)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Load and validate config (with optional path override)
    if args.config_path:
        os.environ["P4_INTEG_CONFIG"] = args.config_path
    cfg = load_config()

    # Apply CLI overrides for exec/ssh
    exec_cfg = cfg.setdefault("exec", {}) if isinstance(cfg.get("exec", {}), dict) else {}
    # --mode
    if getattr(args, "exec_mode", None):
        exec_cfg["mode"] = args.exec_mode
    # --ssh HOST implies ssh mode
    if getattr(args, "ssh_host", None):
        exec_cfg["mode"] = "ssh"
        ssh_cfg = exec_cfg.setdefault("ssh", {})
        ssh_cfg["host"] = args.ssh_host
    else:
        ssh_cfg = exec_cfg.setdefault("ssh", {})
    # Additional SSH parameters
    if getattr(args, "ssh_user", None):
        ssh_cfg["user"] = args.ssh_user
    if getattr(args, "ssh_port", None) is not None:
        ssh_cfg["port"] = int(args.ssh_port)
    if getattr(args, "ssh_key_path", None):
        ssh_cfg["key_path"] = args.ssh_key_path
    if getattr(args, "ssh_password", None):
        ssh_cfg["password"] = args.ssh_password
    if getattr(args, "ssh_shell", None):
        ssh_cfg["shell"] = args.ssh_shell
    if getattr(args, "ssh_login_shell", None) is not None:
        ssh_cfg["login_shell"] = bool(args.ssh_login_shell)
    if getattr(args, "ssh_env_prelude", None):
        ssh_cfg["env_prelude"] = args.ssh_env_prelude
    # Exec workspace root
    if getattr(args, "exec_workspace_root", None):
        exec_cfg["workspace_root"] = args.exec_workspace_root

    cfg["exec"] = exec_cfg

    cfg, warnings, errors = validate_config(cfg)
    # Setup logging after potential overrides
    if args.log_level:
        log_cfg = cfg.setdefault("logging", {}) if isinstance(cfg.get("logging", {}), dict) else {}
        log_cfg["level"] = args.log_level
    _setup_logging(cfg.get("logging", {}))
    for w in warnings:
        print(f"config warning: {w}", file=sys.stderr)
    for e in errors:
        print(f"config error: {e}", file=sys.stderr)

    # Dispatch
    func = getattr(args, "func", None)
    if not func:
        parser.print_help()
        return 2
    return int(func(args, cfg))


if __name__ == "__main__":
    raise SystemExit(main())
