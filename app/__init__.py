from typing import TYPE_CHECKING
from .config import load_config, validate_config
import logging
import os
import asyncio
import threading

from app.master.agent_server import AgentServer
from app.master.job_state_machine import JobStateMachine
from app.master.workspace_queue import workspace_queue
from app.scheduler.scheduler import scheduler_manager

if TYPE_CHECKING:
    from flask import Flask

# Global instances
agent_server = None
state_machine = None


def create_app() -> "Flask":
    from flask import Flask
    global agent_server, state_machine
    app = Flask(__name__)
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-in-prod")
    
    # Register a 'urlencode' filter for templates
    try:
        from urllib.parse import urlencode as _urlencode
        @app.template_filter("urlencode")
        def _jinja_urlencode(value):
            try:
                if isinstance(value, dict):
                    return _urlencode(value, doseq=True)
                return _urlencode(str(value))
            except Exception:
                return _urlencode(str(value))
    except Exception:
        pass

    cfg = load_config()
    cfg, warnings, errors = validate_config(cfg)
    
    # Data dir setup
    try:
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        data_dir = os.environ.get("P4_INTEG_DATA_DIR") or os.path.join(base_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
    except Exception:
        data_dir = os.path.abspath(os.path.join(os.getcwd(), "data"))
        try:
            os.makedirs(data_dir, exist_ok=True)
        except Exception:
            pass

    cfg.setdefault("data_dir", data_dir)
    app.config["DATA_DIR"] = cfg["data_dir"]
    app.config["APP_CONFIG"] = cfg
    
    # Initialize logging
    _setup_logging(cfg.get("logging", {}))
    for w in warnings:
        logging.getLogger("p4_integ").warning("config warning: %s", w)
    for e in errors:
        logging.getLogger("p4_integ").error("config error: %s", e)

    # Initialize Master-Agent Components
    if agent_server is None:
        agent_server = AgentServer(host="0.0.0.0", port=9090)
        state_machine = JobStateMachine(agent_server, cfg)  # Pass config here
        
        # Connect workspace queue to state machine
        state_machine.set_workspace_queue(workspace_queue)
        
        # Start AgentServer in a background thread
        def run_agent_server():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(agent_server.start())
        
        server_thread = threading.Thread(target=run_agent_server, daemon=True)
        server_thread.start()
        
        # Start the scheduler and register job runner
        scheduler_manager.start()
        
        def _schedule_job_runner(schedule_id: str, template_id: str, config: dict):
            """Execute a scheduled job by running its template.
            
            Called by scheduler (cron trigger or Run Now).
            Credentials come from schedule config (saved at create/update time).
            """
            import uuid
            from app.models.template import template_manager
            from app.master.bootstrapper import Bootstrapper
            
            sched_logger = logging.getLogger("ScheduleRunner")
            sched_logger.info(f"Running schedule {schedule_id} with template {template_id}")
            
            # Get credentials from schedule config
            p4_user = config.get('p4_user')
            p4_password = config.get('p4_password')
            if not p4_user or not p4_password:
                raise RuntimeError("Schedule has no P4 credentials. Please edit and save the schedule to refresh credentials.")
            
            # Get template
            template = template_manager.get_template(template_id, username=p4_user)
            if not template:
                raise RuntimeError(f"Template {template_id} not found")
            
            # Build spec from template
            tpl_config = template.get('config', {})
            ssh_config = tpl_config.get('ssh', {})
            
            ssh_host = ssh_config.get('ssh_host')
            ssh_port = ssh_config.get('ssh_port', 22)
            master_host = ssh_config.get('master_host')
            master_port = ssh_config.get('master_port', 9090)
            python_path = ssh_config.get('python_path', 'python3')
            
            if not ssh_host or not master_host:
                raise RuntimeError("Template SSH configuration incomplete (ssh_host or master_host missing)")
            
            workspace = config.get('workspace') or tpl_config.get('workspace')
            if not workspace:
                raise RuntimeError("No workspace configured in template")
            
            spec = {
                "workspace": workspace,
                "branch_spec": tpl_config.get('branch_spec'),
                "changelist": tpl_config.get('changelist'),
                "path": tpl_config.get('path', ''),
                "description": tpl_config.get('description', ''),
                "trial": tpl_config.get('trial', False),
                "p4": {
                    "user": p4_user,
                    "password": p4_password,
                    "client": tpl_config.get('p4', {}).get('client'),
                    "port": tpl_config.get('p4', {}).get('port')
                },
                "connection": {
                    "ssh_host": ssh_host,
                    "ssh_port": ssh_port,
                    "master_host": master_host,
                    "master_port": master_port,
                    "python_path": python_path
                }
            }
            
            # Deploy agent
            ssh_cfg = {
                "host": ssh_host,
                "port": ssh_port,
                "user": p4_user,
                "password": p4_password,
                "python_path": python_path
            }
            
            bootstrapper = Bootstrapper(ssh_cfg)
            pid, agent_hint = bootstrapper.deploy_agent(
                master_host=master_host,
                master_port=master_port,
                workspace=workspace,
                agent_server=agent_server
            )
            
            found_agent = agent_server.wait_for_agent(agent_hint, timeout=15.0)
            if not found_agent:
                raise RuntimeError(f"Agent failed to connect (PID: {pid}, hint: {agent_hint})")
            
            # Create and start job
            job_id = str(uuid.uuid4())
            state_machine.create_job(job_id, found_agent, spec, owner=p4_user)
            asyncio.run_coroutine_threadsafe(
                state_machine.start_job(job_id),
                agent_server._loop
            )
            
            sched_logger.info(f"Schedule {schedule_id} started job {job_id}")
            return job_id
        
        scheduler_manager.set_job_runner(_schedule_job_runner)

    # Register Blueprints
    from .api import bp as api_bp
    app.register_blueprint(api_bp)
    
    # Register existing UI routes (refactored to use new architecture)
    # Note: We might need to adapt server.py or replace it
    from .server import register_routes
    register_routes(app, state_machine)  # Pass state_machine instead of job_manager
    
    return app


def _setup_logging(logging_cfg: dict) -> None:
    try:
        import logging
        from logging.handlers import RotatingFileHandler
        level_name = str(logging_cfg.get("level", "INFO")).upper()
        level = getattr(logging, level_name, logging.INFO)
        logger = logging.getLogger("p4_integ")
        if logger.handlers:
            return
        logger.setLevel(level)
        
        fmt = logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        
        ch = logging.StreamHandler()
        ch.setLevel(level)
        ch.setFormatter(fmt)
        logger.addHandler(ch)
        
        log_file = logging_cfg.get("file") or os.path.join("data", "p4_integ.log")
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            fh = RotatingFileHandler(
                log_file, 
                maxBytes=int(logging_cfg.get("max_bytes", 2 * 1024 * 1024)), 
                backupCount=int(logging_cfg.get("backup_count", 5))
            )
            fh.setLevel(level)
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except Exception:
            pass
    except Exception:
        pass
