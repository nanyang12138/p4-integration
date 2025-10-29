import json
import os
import threading
import time
from typing import Dict, Any, Optional


class JobStorage:
    def __init__(self, data_dir: str = "data"):
        self._data_dir = data_dir
        self._path = os.path.join(self._data_dir, "jobs.json")
        self._lock = threading.Lock()
        # In-memory cache for performance
        self._cache: Dict[str, Any] = {}
        self._cache_loaded = False
        self._dirty = False  # Track if cache needs to be written
        self._last_write = 0.0
        self._write_interval = 2.0  # Lazy write: batch writes every 2 seconds
        os.makedirs(self._data_dir, exist_ok=True)
        if not os.path.exists(self._path) or os.path.getsize(self._path) == 0:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump({}, f)
        # Start background writer thread for lazy writes
        self._writer_thread = threading.Thread(target=self._background_writer, daemon=True)
        self._running = True
        self._writer_thread.start()

    def _load_cache(self) -> None:
        """Load from disk into cache (called once on first access)."""
        with self._lock:
            if self._cache_loaded:
                return
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    text = f.read()
                if not text.strip():
                    self._cache = {}
                else:
                    data = json.loads(text)
                    self._cache = data if isinstance(data, dict) else {}
            except Exception:
                # Corrupted or partially written file; start with empty cache
                self._cache = {}
            self._cache_loaded = True

    def _read(self) -> Dict[str, Any]:
        """Legacy method for backward compatibility - now reads from cache."""
        self._load_cache()
        with self._lock:
            return dict(self._cache)  # Return copy to prevent external modification

    def _write_to_disk(self, data: Dict[str, Any]) -> None:
        """Write to disk (must be called with lock held)."""
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)
        self._last_write = time.time()
        self._dirty = False
    
    def _background_writer(self) -> None:
        """Background thread that writes dirty cache to disk periodically."""
        while self._running:
            time.sleep(self._write_interval)
            with self._lock:
                if self._dirty and time.time() - self._last_write >= self._write_interval:
                    try:
                        self._write_to_disk(dict(self._cache))
                    except Exception:
                        pass  # Silently ignore write errors in background thread

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        self._load_cache()
        with self._lock:
            job = self._cache.get(job_id)
            return dict(job) if job else None  # Return copy

    def put(self, job_id: str, job: Dict[str, Any]) -> None:
        self._load_cache()
        with self._lock:
            self._cache[job_id] = dict(job)  # Store copy
            self._dirty = True

    def all(self) -> Dict[str, Any]:
        self._load_cache()
        with self._lock:
            return {k: dict(v) for k, v in self._cache.items()}  # Return deep copy
    
    def flush(self) -> None:
        """Force immediate write of dirty cache to disk."""
        with self._lock:
            if self._dirty:
                self._write_to_disk(dict(self._cache))
    
    def stop(self) -> None:
        """Stop background writer and flush any pending changes."""
        self._running = False
        if hasattr(self, '_writer_thread') and self._writer_thread.is_alive():
            self._writer_thread.join(timeout=5.0)
        self.flush()
