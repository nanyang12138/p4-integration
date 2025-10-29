import os
from typing import Dict


def _setup_logging(logging_cfg: Dict) -> None:
    """
    Lightweight logging setup used by CLI (no Flask dependency).
    Mirrors the behavior of _setup_logging in app.__init__, but lives
    in a separate module so importing it doesn't import Flask.
    """
    try:
        import logging
        from logging.handlers import RotatingFileHandler

        level_name = str(logging_cfg.get("level", "INFO")).upper()
        level = getattr(logging, level_name, logging.INFO)
        logger = logging.getLogger("p4_integ")

        # If already configured, don't add duplicate handlers
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
        try:
            os.makedirs(os.path.dirname(str(log_file)), exist_ok=True)
        except Exception:
            pass
        fh = RotatingFileHandler(
            str(log_file),
            maxBytes=int(logging_cfg.get("max_bytes", 2 * 1024 * 1024)),
            backupCount=int(logging_cfg.get("backup_count", 5)),
        )
        fh.setLevel(level)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        # Fall back silently to default logging
        pass
