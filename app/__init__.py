from typing import TYPE_CHECKING
from .config import load_config, validate_config
import logging
import os
import asyncio
import threading

from app.master.agent_server import AgentServer
from app.master.job_state_machine import JobStateMachine

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
        
        # Start AgentServer in a background thread
        def run_agent_server():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(agent_server.start())
        
        server_thread = threading.Thread(target=run_agent_server, daemon=True)
        server_thread.start()

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
