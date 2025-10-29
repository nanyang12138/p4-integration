import os
import time
import json
from typing import Dict, Any, Optional, List

from .live import broker
from .config import load_config


JobEvent = Dict[str, Any]


def events_path(job_id: str) -> str:
    # Resolve data dir from env or config to stay consistent with server paths
    base_data = os.environ.get("P4_INTEG_DATA_DIR") or load_config().get("data_dir") or "data"
    data_dir = os.path.join(str(base_data), "artifacts")
    job_dir = os.path.join(data_dir, str(job_id))
    os.makedirs(job_dir, exist_ok=True)
    return os.path.join(job_dir, "events.jsonl")


def emit_event(job_id: str, event: str, status: Optional[str] = None, stage: Optional[str] = None, data: Optional[Dict[str, Any]] = None) -> None:
    try:
        now = int(time.time())
        record: JobEvent = {
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "job_id": job_id,
            "event": event,
        }
        if status is not None:
            record["status"] = status
        if stage is not None:
            record["stage"] = stage
        if data:
            for k, v in data.items():
                record[k] = v
        path = events_path(job_id)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        try:
            broker.publish(record)
        except Exception:
            pass
    except Exception:
        pass


def read_events(job_id: str) -> List[JobEvent]:
    out: List[JobEvent] = []
    try:
        path = events_path(job_id)
        if not os.path.exists(path):
            return out
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    out.append(json.loads(s))
                except Exception:
                    pass
        return out
    except Exception:
        return out
