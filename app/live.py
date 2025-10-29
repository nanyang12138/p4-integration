import queue
import threading
import time
from typing import Dict, Any, Generator, Optional, List


class EventBroker:
    def __init__(self) -> None:
        self._subs: List["queue.Queue[Dict[str, Any]]"] = []
        self._lock = threading.Lock()

    def subscribe(self) -> "queue.Queue[Dict[str, Any]]":
        q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[Dict[str, Any]]") -> None:
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def publish(self, event: Dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except Exception:
                pass


broker = EventBroker()


def to_sse(event: Dict[str, Any]) -> str:
    import json
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def sse_stream(filter_job_id: Optional[str] = None) -> Generator[str, None, None]:
    q = broker.subscribe()
    try:
        # initial heartbeat
        yield ":ok\n\n"
        last_beat = time.time()
        while True:
            try:
                ev = q.get(timeout=5.0)
                if filter_job_id and str(ev.get("job_id")) != str(filter_job_id):
                    continue
                yield to_sse(ev)
            except queue.Empty:
                # heartbeat every 15s to keep connection alive
                if time.time() - last_beat > 15:
                    yield ":keepalive\n\n"
                    last_beat = time.time()
    finally:
        broker.unsubscribe(q)


