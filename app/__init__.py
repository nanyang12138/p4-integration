from typing import TYPE_CHECKING
from .config import load_config, validate_config
from .jobs import JobManager
import logging
import os

if TYPE_CHECKING:
    from flask import Flask

job_manager = None


def create_app() -> "Flask":
    from flask import Flask
    global job_manager
    app = Flask(__name__)
    # Register a 'urlencode' filter for templates (used in job_detail.html)
    try:
        from urllib.parse import urlencode as _urlencode  # type: ignore
        @app.template_filter("urlencode")
        def _jinja_urlencode(value):
            try:
                if isinstance(value, dict):
                    # Support dict -> query string (doseq to handle lists)
                    return _urlencode(value, doseq=True)  # type: ignore
                # Fallback to string encode
                return _urlencode(str(value))
            except Exception:
                return _urlencode(str(value))
    except Exception:
        # Fail silently; templates just won't use the filter
        pass
    cfg = load_config()
    cfg, warnings, errors = validate_config(cfg)
    # Compute absolute data dir under application root (or env override)
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
    # Attach to config and app
    cfg.setdefault("data_dir", data_dir)
    app.config["DATA_DIR"] = cfg["data_dir"]
    app.config["APP_CONFIG"] = cfg
    # Normalize logging file path into DATA_DIR if relative
    try:
        log_cfg = cfg.setdefault("logging", {})
        lf = log_cfg.get("file")
        if not lf or not os.path.isabs(str(lf)):
            log_cfg["file"] = os.path.join(data_dir, os.path.basename(str(lf or "p4_integ.log")))
    except Exception:
        pass
    # Initialize logging once per process
    _setup_logging(cfg.get("logging", {}))
    for w in warnings:
        logging.getLogger("p4_integ").warning("config warning: %s", w)
    for e in errors:
        logging.getLogger("p4_integ").error("config error: %s", e)
    if job_manager is None:
        job_manager = JobManager(app.config["APP_CONFIG"])  # type: ignore
    from .server import register_routes  # lazy import to avoid Flask on CLI import
    register_routes(app, job_manager)
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
        # Formatter (optionally JSON)
        json_mode = bool(logging_cfg.get("json", False))
        if json_mode:
            class _JsonFormatter(logging.Formatter):
                def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
                    import json as _json
                    payload = {
                        "ts": self.formatTime(record, "%Y-%m-%d %H:%M:%S"),
                        "level": record.levelname,
                        "logger": record.name,
                        "msg": record.getMessage(),
                    }
                    if record.exc_info:
                        payload["exc_info"] = self.formatException(record.exc_info)
                    return _json.dumps(payload, ensure_ascii=False)
            fmt = _JsonFormatter()
        else:
            fmt = logging.Formatter(
                fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        # Console handler
        ch = logging.StreamHandler()
        ch.setLevel(level)
        ch.setFormatter(fmt)
        logger.addHandler(ch)
        # Rotating file handler
        log_file = logging_cfg.get("file") or os.path.join("data", "p4_integ.log")
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=int(logging_cfg.get("max_bytes", 2 * 1024 * 1024)), backupCount=int(logging_cfg.get("backup_count", 5)))
        fh.setLevel(level)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        # Fall back silently to default logging
        pass
