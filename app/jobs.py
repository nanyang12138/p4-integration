import threading
import queue
import time
import uuid
import os
from typing import Dict, Any, Optional, List, Callable, Tuple
import logging
from .events import emit_event

from .p4_client import P4Client, P4Error
from .storage import JobStorage


Job = Dict[str, Any]
def _list_artifacts(job_dir: str) -> List[str]:
    try:
        files = []
        for name in os.listdir(job_dir):
            if name.endswith(".log") or name.endswith(".jsonl") or name == "status.json":
                files.append(name)
        return sorted(files)
    except Exception:
        return []


def _emit(job: Job, event: str, data: Optional[Dict[str, Any]] = None) -> None:
    try:
        emit_event(str(job.get("id")), event, status=str(job.get("status")), stage=str(job.get("stage")), data=data or {})
    except Exception:
        pass


# Removed: Moved to env_helper.py for centralization



class JobManager:
    def __init__(self, config: Dict[str, Any]):
        self._config = config
        self._p4 = P4Client(config)
        # Absolute, configurable data dir (set in app.__init__)
        self._data_dir = str(config.get("data_dir", "data"))
        self._storage = JobStorage(self._data_dir)
        # Simplified single queue system
        self._jobs_queue: "queue.Queue[str]" = queue.Queue()
        self._max_queue_size = int(config.get("max_queue_size", 100))
        self._cancelled_ids: set[str] = set()
        # Job-level locks to prevent concurrent access
        self._job_locks: Dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()  # Protects the _job_locks dict itself
        # Track jobs being manually resolved (pause auto-resolve for these)
        self._manual_resolve_jobs: set[str] = set()
        # Conflict check cache and debounce tracking
        self._conflict_cache: Dict[str, Tuple[float, List[str]]] = {}  # {job_id: (timestamp, conflicts)}
        self._cache_ttl = 30  # 30 seconds cache
        self._thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._running = True
        self._thread.start()
        self._logger = logging.getLogger("p4_integ.jobs")
        
        # Start auto-resolve refresher thread if enabled
        auto_resolve_cfg = self._config.get("auto_resolve", {})
        self._auto_resolve_enabled = auto_resolve_cfg.get("enabled", True) if isinstance(auto_resolve_cfg, dict) else True
        self._auto_resolve_interval = auto_resolve_cfg.get("interval", 60) if isinstance(auto_resolve_cfg, dict) else 60
        
        if self._auto_resolve_enabled:
            self._refresher_thread = threading.Thread(target=self._resolve_refresher_loop, daemon=True)
            self._refresher_thread.start()
            self._logger.info(f"Auto-resolve refresher started (interval: {self._auto_resolve_interval}s)")
        else:
            self._refresher_thread = None
            self._logger.info("Auto-resolve refresher disabled")

    def stop(self) -> None:
        self._running = False

    def is_worker_alive(self) -> bool:
        return self._thread.is_alive()

    def ensure_worker(self) -> None:
        if not self._thread.is_alive():
            # Wait for old thread to finish to avoid race
            if hasattr(self, '_thread') and self._thread.is_alive():
                self._thread.join(timeout=1.0)
            self._thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._running = True
            self._thread.start()

    def enqueue(self, job_id: str) -> None:
        # Check queue size limit
        if self._jobs_queue.qsize() >= self._max_queue_size:
            raise ValueError(f"Queue full (max {self._max_queue_size})")
        self._jobs_queue.put(job_id)
    
    def _get_job_lock(self, job_id: str) -> threading.Lock:
        """Get or create a lock for a specific job."""
        with self._locks_lock:
            if job_id not in self._job_locks:
                self._job_locks[job_id] = threading.Lock()
            return self._job_locks[job_id]
    
    def _cleanup_job_lock(self, job_id: str) -> None:
        """Remove lock for completed job to free memory."""
        with self._locks_lock:
            self._job_locks.pop(job_id, None)
            # Also cleanup cancelled_ids for this job
            self._cancelled_ids.discard(job_id)
            # Clear conflict cache
            self._conflict_cache.pop(job_id, None)

    def create_job(self, payload: Dict[str, Any]) -> str:
        job_id = str(uuid.uuid4())
        
        # Generate readable ID (INT-YYYYMMDD-NNN)
        from datetime import datetime
        today = datetime.now().strftime('%Y%m%d')
        
        # Count today's jobs to get sequence number
        all_jobs = self._storage.all()
        today_jobs = [j for j in all_jobs.values() 
                      if j.get('readable_id', '').startswith(f'INT-{today}-')]
        seq = len(today_jobs) + 1
        readable_id = f"INT-{today}-{seq:03d}"
        
        job: Job = {
            "id": job_id,
            "readable_id": readable_id,
            "status": "queued",
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
            "payload": payload,
            "log": [],
            "changelist": None,
            "conflicts": [],
            "blocked_files": [],
            "stage": "pending",
        }
        self._storage.put(job_id, job)
        # Immediately enqueue and let worker pick it up
        self.ensure_worker()
        self.enqueue(job_id)
        return job_id

    def get_job(self, job_id: str) -> Optional[Job]:
        job = self._storage.get(job_id)
        if job is None:
            return None
        # Backward-compatible normalization for status
        try:
            s = str(job.get("status") or "")
            if s == "submitted":
                job["status"] = "pushed"
            elif s == "submit" or s == "pre_submit_checks" or s == "ready_to_submit" or s == "awaiting_approval":
                job["status"] = "running"
        except Exception:
            pass
        if not job.get("stage"):
            job["stage"] = self._infer_stage(job)
        return job

    def list_jobs(self) -> List[Job]:
        jobs = list(self._storage.all().values())
        for j in jobs:
            try:
                s = str(j.get("status") or "")
                if s == "submitted":
                    j["status"] = "pushed"
                elif s == "submit" or s == "pre_submit_checks" or s == "ready_to_submit" or s == "awaiting_approval":
                    j["status"] = "running"
            except Exception:
                pass
            if not j.get("stage"):
                j["stage"] = self._infer_stage(j)
        return jobs

    def _append_log(self, job: Job, text: str) -> None:
        job["log"].append(self._redact(text))
        job["updated_at"] = int(time.time())
        self._storage.put(job["id"], job)
        try:
            _emit(job, "log", {"message": text})
        except Exception:
            pass

    def _save(self, job: Job) -> None:
        self._storage.put(job["id"], job)
        try:
            self._persist_status(job)
        except Exception:  # noqa: BLE001
            pass

    # Removed webhook and email notifications to simplify code

    def _persist_log_artifacts(self, job: Job, label: str, content: str) -> None:
        data_dir = os.path.join(self._data_dir, "artifacts")
        job_dir = os.path.join(data_dir, str(job["id"]))
        os.makedirs(job_dir, exist_ok=True)
        path = os.path.join(job_dir, f"{label}.log")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._redact(content))
            self._append_log(job, f"Saved {label} log to {path}")
        except Exception as e:  # noqa: BLE001
            self._append_log(job, f"Failed to save {label} log: {e}")

    def _stream_log_artifact(self, job: Job, label: str, on_chunk: Callable[[str], None]) -> Callable[[str, str], None]:
        data_dir = os.path.join(self._data_dir, "artifacts")
        job_dir = os.path.join(data_dir, str(job["id"]))
        os.makedirs(job_dir, exist_ok=True)
        path = os.path.join(job_dir, f"{label}.log")
        # open append and stream in real time
        f = open(path, "a", encoding="utf-8")
        def _cb(stream: str, chunk: str) -> None:
            try:
                text = self._redact(chunk)
                f.write(text)
                f.flush()
                on_chunk(text)
            except Exception:
                pass
        def _finalize() -> None:
            try:
                f.close()
            except Exception:
                pass
        # Return a wrapped callback that also handles close when called with tag 'close'
        def _wrapped(tag: str, data: str) -> None:
            if tag == '__close__':
                _finalize()
                return
            _cb(tag, data)
        return _wrapped

    def _shelve_with_name_check_simple(self, job: Job, changelist: int) -> None:
        """Simplified shelve with basic name_check handling."""
        try:
            out, err = self._p4.shelve_first(changelist)
            self._append_log(job, f"Shelve output: {out}")
            if err:
                self._append_log(job, f"Shelve stderr: {err}")
            
            if "name_check" in (out + err).lower():
                self._append_log(job, "Detected name_check, attempting remediation")
                self._name_check_remediate(job, changelist)
        except P4Error as e:
            if "name_check" in str(e).lower():
                self._append_log(job, "Shelve failed with name_check, attempting remediation")
                self._name_check_remediate(job, changelist)
            else:
                raise

    def _name_check_remediate(self, job: Job, changelist: int) -> None:
        """If name_check appears in shelve logs, revert offending files and re-shelve."""
        try:
            ws = self._p4.workspace_root
            import os as _os, shlex as _shlex
            p4bin = _shlex.quote(self._p4.p4_bin or "p4")
            env_inline = " ".join([f"{k}={_shlex.quote(str(v))}" for k, v in self._p4._env().items()]) or ""
            script_path = _os.path.join(ws, ".p4_integ_name_check.sh")
            
            # Use centralized env init helper
            from .env_helper import EnvInitHelper
            env_helper = EnvInitHelper(self._config)
            name_check_tool = env_helper.get_name_check_tool_path()
            init_commands = env_helper.get_init_commands_multiline()
            
            # Build a standalone bash script to avoid default csh/tcsh interpreting our syntax.
            script_content = f"""#!/usr/bin/env bash
# name_check remediation script (bash)
echo 'NC: start name_check remediation'
cd {ws} || {{ echo 'NC: cd failed'; exit 1; }}
# Initialize environment for p4
{init_commands}
echo '--- name_check remediation loop starting ---'
tries=0
while true; do
  tries=$((tries+1))
  echo "pass=$tries: scanning shelve output for name_check offenders"
  /bin/rm -f name_check_file_list || true
  # Pipe shelve output to name_check_file_list extractor
  {env_inline} {p4bin} shelve -c {int(changelist)} 2>&1 | {name_check_tool} > name_check_file_list || true
  if [ -s name_check_file_list ]; then
    echo 'offending files:'
    cat name_check_file_list
    echo 'reverting offending files'
    {env_inline} {p4bin} -c "{self._p4.client}" -x name_check_file_list revert || true
    echo 'reshelving (-r)'
    {env_inline} {p4bin} shelve -r -c {int(changelist)} || true
  else
    echo 'name_check_remediated: no offending files'
    break
  fi
  # Safety guard to avoid infinite loop
  if [ $tries -ge 5 ]; then
    echo 'name_check_remediated: reached max passes (5), stopping'
    break
  fi
done
"""
            # Write the script file atomically under the workspace
            self._p4.write_file_text(script_path, script_content)
            # Prepare the exact command to run with bash
            run_script = (
                f"echo 'NC: launching {_shlex.quote(script_path)}'; "
                f"chmod +x {_shlex.quote(script_path)} || true; "
                f"rc=0; /usr/bin/env bash {_shlex.quote(script_path)} || rc=$?; "
                f"echo 'NC: script exit='${{rc}}; exit $rc"
            )
            def name_check_emitter(tag: str, data: str):
                if "PID:" in data:
                    # Extract PID from log output
                    import re
                    pid_match = re.search(r'PID:\s*(\d+)', data)
                    if pid_match:
                        token = pid_match.group(1)
                        try:
                            job.setdefault("pids", {})["name_check"] = int(token)
                            self._save(job)
                        except Exception:
                            pass
                # Pass through to original emitter
                emitter(tag, data)
                _emit(job, "log", {"message": data})
            
            emitter = self._stream_log_artifact(job, "name_check", lambda text: _emit(job, "log", {"message": text}))
            # Run with bash explicitly and pass P4 env via process env to avoid in-script exports
            try:
                # Ensure the artifact is never empty
                emitter("stdout", "NC: starting remediation\n")
                code = self._p4.runner.run_script_stream(
                    run_script,
                    shell_path="/bin/bash",
                    cwd=self._p4.workspace_root,
                    env=self._p4._env(),
                    input_text=None,
                    on_chunk=name_check_emitter,
                )  # type: ignore
            except Exception as e2:
                try:
                    emitter("stderr", f"NC: run_script_stream exception: {e2}\n")
                except Exception:
                    pass
                raise
            finally:
                emitter("__close__", "")
            # Persist file list as an artifact if accessible
            try:
                content = self._p4.read_file_text(os.path.join(self._p4.workspace_root, "name_check_file_list"))
                if content:
                    self._persist_log_artifacts(job, "name_check_file_list", content)
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            self._append_log(job, f"name_check remediation failed: {e}")

    # Removed auto-resolve refresher to simplify code

    def _set_stage(self, job: Job, stage: str) -> None:
        changed = (str(job.get("stage")) != stage)
        if changed:
            job["stage"] = stage
            self._append_log(job, f"Stage -> {stage}")
        else:
            job["stage"] = stage
        self._save(job)
        try:
            if changed:
                _emit(job, "stage", {"stage": stage})
        except Exception:
            pass

    def _persist_status(self, job: Job) -> None:
        import json
        data_dir = os.path.join(self._data_dir, "artifacts")
        job_dir = os.path.join(data_dir, str(job["id"]))
        os.makedirs(job_dir, exist_ok=True)
        path = os.path.join(job_dir, "status.json")
        snapshot = {
            "id": job.get("id"),
            "status": job.get("status"),
            "stage": job.get("stage"),
            "changelist": job.get("changelist"),
            "conflicts": job.get("conflicts", []),
            "blocked_files": job.get("blocked_files", []),
            "updated_at": job.get("updated_at"),
            "created_at": job.get("created_at"),
            "payload": job.get("payload", {}),
            "pids": job.get("pids", {}),
            "artifacts": _list_artifacts(job_dir),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

    def _redact(self, s: str) -> str:
        try:
            redacted = s or ""
            secrets: List[str] = []
            if self._config.get("p4w_password"):
                secrets.append(str(self._config.get("p4w_password")))
            p4cfg = self._config.get("p4", {}) if isinstance(self._config.get("p4", {}), dict) else {}
            if isinstance(p4cfg, dict) and p4cfg.get("password"):
                secrets.append(str(p4cfg.get("password")))
            for sec in secrets:
                if sec:
                    redacted = redacted.replace(sec, "***")
            return redacted
        except Exception:
            return s

    def _infer_stage(self, job: Job) -> str:
        try:
            # Prefer explicit stage from logs
            for line in reversed(job.get("log", [])):
                if isinstance(line, str) and line.startswith("Stage -> "):
                    return line.split("Stage -> ", 1)[1].strip()
            status = str(job.get("status", ""))
            if status == "needs_resolve":
                return "resolve"
            if status == "blocked":
                return "blocked"
            if status == "submitted":
                return "submitted"
            if status == "running":
                logs = "\n".join(job.get("log", []))
                if "Integrating " in logs:
                    return "integrate"
                if "pre-commands exit" in logs or "Running pre-commands" in logs:
                    return "sync"
                return "running"
            if status == "queued":
                # If it's queued but worker alive, treat as pending only very briefly
                return "pending"
            return status or "pending"
        except Exception:
            return str(job.get("status", "pending"))

    def _resolve_refresher_loop(self) -> None:
        """Background thread that periodically checks jobs in needs_resolve status."""
        import time
        while self._running:
            try:
                time.sleep(self._auto_resolve_interval)
                
                # Only check needs_resolve status jobs that are not being manually resolved
                for job in self.list_jobs():
                    job_id = job["id"]
                    if job.get("status") == "needs_resolve" and job_id not in self._manual_resolve_jobs:
                        try:
                            self._auto_check_and_continue(job_id)
                        except Exception as e:
                            self._logger.debug(f"Auto-check failed for job {job_id}: {e}")
            except Exception as e:
                self._logger.error(f"Refresher loop error: {e}")
    
    def _get_conflicts_with_cache(self, job_id: str) -> Tuple[List[str], str]:
        """Get conflicts with 30-second cache to avoid redundant p4 calls."""
        # Check cache
        cached = self._conflict_cache.get(job_id)
        if cached:
            timestamp, conflicts = cached
            if time.time() - timestamp < self._cache_ttl:
                self._logger.debug(f"Job {job_id}: using cached conflicts ({len(conflicts)} files)")
                return conflicts, "(cached)"
        
        # Cache miss or expired - execute actual check
        self._logger.debug(f"Job {job_id}: cache miss/expired, executing p4 resolve -n")
        conflicts, log = self._p4.resolve_preview_shell()
        self._conflict_cache[job_id] = (time.time(), conflicts)
        return conflicts, log
    
    def _auto_check_and_continue(self, job_id: str) -> None:
        """Automatically check resolve status and continue if conflicts are cleared."""
        lock = self._get_job_lock(job_id)
        # Try to acquire lock with timeout to avoid blocking auto-resolve thread
        acquired = lock.acquire(blocking=True, timeout=0.5)
        if not acquired:
            self._logger.debug(f"Job {job_id} is locked (manual operation in progress), skipping auto-check")
            return
        try:
            job = self.get_job(job_id)
            if not job or job.get("status") != "needs_resolve":
                return
            
            # Execute resolve preview to check current conflicts (with cache)
            self._logger.info(f"Auto-checking resolve status for job {job_id}")
            conflicts, _ = self._get_conflicts_with_cache(job_id)
            
            if not conflicts:  # No conflicts found, can proceed
                self._append_log(job, "Auto-check: conflicts cleared, automatically continuing to submit")
                self._logger.info(f"Job {job_id}: conflicts cleared by auto-check, triggering continue_to_submit")
                # Clear cache when continuing
                self._conflict_cache.pop(job_id, None)
                # Call new continue_to_submit API instead of rescan_conflicts
                self._continue_to_submit(job)
            else:
                self._logger.debug(f"Job {job_id}: still has {len(conflicts)} conflicts")
        except Exception as e:
            self._logger.debug(f"Auto-check failed for {job_id}: {e}")
        finally:
            lock.release()

    def _worker_loop(self) -> None:
        while self._running:
            try:
                job_id = self._jobs_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            job = self.get_job(job_id)
            if not job:
                self._logger.warning("Dequeued unknown job_id=%s", job_id)
                continue
            # Skip cancelled jobs quickly
            if job.get("status") == "cancelled" or job_id in self._cancelled_ids:
                self._append_log(job, "Job cancelled before start")
                self._logger.info("Skip cancelled job_id=%s", job_id)
                self._cleanup_job_lock(job_id)
                continue
            
            # Acquire job lock for the entire execution
            lock = self._get_job_lock(job_id)
            with lock:
                self._logger.info("Start job_id=%s", job_id)
                self._run_job(job)
                self._logger.info("End job_id=%s status=%s stage=%s", job_id, job.get("status"), job.get("stage"))
                self._save(job)
                
                # Cleanup lock if job reached terminal state
                terminal_states = {"pushed", "pushed_trial", "error", "cancelled", "blocked"}
                if job.get("status") in terminal_states:
                    # Will cleanup after releasing lock
                    pass
            
            # Cleanup outside the lock
            if job.get("status") in {"pushed", "pushed_trial", "error", "cancelled", "blocked"}:
                self._cleanup_job_lock(job_id)
            
            try:
                self._jobs_queue.task_done()
            except Exception:
                pass

    def _check_job_cancelled(self, job: Job) -> bool:
        """Check if job has been cancelled and should stop execution."""
        job_id = str(job.get("id", ""))
        if job.get("status") == "cancelled" or job_id in self._cancelled_ids:
            self._append_log(job, "Job cancelled during execution")
            job["status"] = "cancelled"
            self._save(job)
            return True
        return False

    def _run_job(self, job: Job) -> None:
        job["status"] = "running"
        self._append_log(job, "Job started")
        try:
            _emit(job, "status", {"status": "running"})
        except Exception:
            pass
        # Log workspace details
        exec_mode = getattr(self._p4, "exec_mode", "local")
        self._append_log(job, f"Workspace: {self._p4.workspace_root} (client={self._p4.client}, exec={exec_mode})")
        self._logger.debug(
            "Job %s workspace_root=%s client=%s exec=%s",
            job.get("id"), self._p4.workspace_root, self._p4.client, exec_mode,
        )
        payload = job.get("payload", {})
        assert isinstance(payload, dict)

        source = str(payload.get("source", ""))
        target = str(payload.get("target", ""))
        description = str(payload.get("description", "Integration job"))
        raw_changelist = payload.get("changelist")
        spec_name = str(payload.get("spec", "")).strip()
        specs = self._config.get("specs", {}) if isinstance(self._config.get("specs", {}), dict) else {}
        branch_spec: Optional[str] = None
        source_rev_change: Optional[int] = None
        if spec_name:
            if isinstance(specs, dict) and spec_name in specs:
                spec_def = specs.get(spec_name) or {}
                if isinstance(spec_def, dict):
                    # Keep for logging only; not required for -b mode
                    s_spec = str(spec_def.get("source", ""))
                    t_spec = str(spec_def.get("target", ""))
                    if s_spec and t_spec:
                        self._append_log(job, f"Using spec '{spec_name}' (branch view): {s_spec} -> {t_spec}")
                    spec_desc = spec_def.get("description")
                    if isinstance(spec_desc, str) and spec_desc.strip():
                        description = spec_desc
                    branch_spec = spec_name
            else:
                self._append_log(job, f"WARNING: Spec '{spec_name}' not found in configuration, treating as branch spec anyway")
                branch_spec = spec_name
        # Interpret provided changelist as SOURCE revision to integrate from (revRange) for both modes
        if raw_changelist:
            try:
                source_rev_change = int(raw_changelist)
                self._append_log(job, f"Using source rev @{source_rev_change} for integrate")
            except Exception:  # noqa: BLE001
                pass

        try:
            # Check if job was cancelled before starting
            if self._check_job_cancelled(job):
                return
                
            # Pre-commands in workspace (both ssh and local Linux)
            self._set_stage(job, "sync")
            
            # Use centralized env init helper
            from .env_helper import EnvInitHelper
            env_helper = EnvInitHelper(self._config)
            ws = self._p4.workspace_root
            
            if env_helper.enabled:
                self._append_log(job, f"Running pre-commands (bash): cd <ws> -> source {env_helper.init_script} -> {env_helper.bootenv_cmd} -> p4w sync_all -bsc")
                script = (
                    f"cd {ws}; echo CWD:$(pwd); ls -ld .; "
                    f"echo STEP:init; source {env_helper.init_script} || exit 1; echo OK:init; "
                    f"echo STEP:{env_helper.bootenv_cmd}; {env_helper.bootenv_cmd} || exit 1; echo OK:{env_helper.bootenv_cmd}; "
                    # Launch p4w in background to capture its real PID, then wait and propagate its exit code
                    "echo STEP:p4w; ( p4w sync_all -bsc ) & pid=$!; echo PID:$pid; wait $pid; rc=$?; echo RC:$rc; exit $rc"
                )
            else:
                self._append_log(job, "Skipping environment initialization (env_init.enabled=false)")
                script = (
                    f"cd {ws}; echo CWD:$(pwd); ls -ld .; "
                    # Launch p4w in background to capture its real PID, then wait and propagate its exit code
                    "echo STEP:p4w; ( p4w sync_all -bsc ) & pid=$!; echo PID:$pid; wait $pid; rc=$?; echo RC:$rc; exit $rc"
                )
            # Log the exact script executed for sync stage
            try:
                self._append_log(job, "CMD(sync): " + script)
            except Exception:
                pass
            pw = str(self._config.get("p4w_password", ""))
            input_text = pw + "\n" if pw else None
            # streaming capture with PID detection
            def _emit_pre(text: str) -> None:
                try:
                    # capture PID early when it appears
                    if text.startswith("PID:"):
                        token = text.strip().split(":",1)[1].strip()
                        if token.isdigit():
                            job.setdefault("pids", {})["pre"] = int(token)
                            self._save(job)
                    _emit(job, "log", {"message": text})
                except Exception:
                    pass
            emitter = self._stream_log_artifact(job, "pre", _emit_pre)
            code = self._p4.runner.run_script_stream(script, shell_path="/bin/bash", cwd=self._p4.workspace_root, env=None, input_text=input_text, on_chunk=emitter)  # type: ignore
            emitter("__close__", "")
            self._append_log(job, f"pre-commands exit={code}")
            if code != 0:
                self._set_stage(job, "error")
                job["status"] = "error"
                self._save(job)
                return

            # Use default changelist for integrate/resolve; CL will be created later when needed
            self._append_log(job, "Using default changelist")

            # Check if job was cancelled before integrate
            if self._check_job_cancelled(job):
                return
                
            # Integrate (or preview)
            if branch_spec or (source and target):
                self._set_stage(job, "integrate")
                self._append_log(job, f"Integration parameters: branch_spec='{branch_spec}', source='{source}', target='{target}'")
                # Prefer branch spec if provided
                # Streaming p4 integrate output to integrate_out.log
                def _emit_line(text: str) -> None:
                    try:
                        # capture PID early when it appears
                        if text.startswith("PID:"):
                            token = text.strip().split(":",1)[1].strip()
                            if token.isdigit():
                                job.setdefault("pids", {})["integrate"] = int(token)
                                self._save(job)
                        _emit(job, "log", {"message": text})
                    except Exception:
                        pass
                integrate_cb = self._stream_log_artifact(job, "integrate_out", _emit_line)
                try:
                    if branch_spec:
                        if source_rev_change:
                            # Log integrate command for branch spec with change
                            self._append_log(job, f"CMD(integrate): p4 integrate -b {branch_spec} ...@{int(source_rev_change)}")
                            self._append_log(job, f"Integrating via branch -b {branch_spec} ...@{source_rev_change}")
                            code = self._p4.run_p4_stream(["integrate", "-b", branch_spec, f"...@{int(source_rev_change)}"], integrate_cb)
                        else:
                            # Log integrate command for branch spec latest
                            self._append_log(job, f"CMD(integrate): p4 integrate -b {branch_spec}")
                            self._append_log(job, f"Integrating via branch -b {branch_spec} (latest)")
                            code = self._p4.run_p4_stream(["integrate", "-b", branch_spec], integrate_cb)
                    else:
                        src_arg = source + (f"@{source_rev_change}" if source_rev_change else "")
                        # Log integrate command for explicit source/target
                        self._append_log(job, f"CMD(integrate): p4 integrate {src_arg} {target}")
                        self._append_log(job, f"Integrating {src_arg} -> {target}")
                        code = self._p4.run_p4_stream(["integrate", src_arg, target], integrate_cb)
                except Exception as e:
                    if "SSH session not active" in str(e):
                        try:
                            self._append_log(job, "SSH dropped during integrate; reconnecting and retrying once")
                            # reset SSH client
                            try:
                                if hasattr(self._p4, 'runner') and hasattr(self._p4.runner, '_client'):
                                    self._p4.runner._client = None  # type: ignore[attr-defined]
                            except Exception:
                                pass
                            # retry once
                            if branch_spec:
                                if source_rev_change:
                                    code = self._p4.run_p4_stream(["integrate", "-b", branch_spec, f"...@{int(source_rev_change)}"], integrate_cb)
                                else:
                                    code = self._p4.run_p4_stream(["integrate", "-b", branch_spec], integrate_cb)
                            else:
                                src_arg = source + (f"@{source_rev_change}" if source_rev_change else "")
                                code = self._p4.run_p4_stream(["integrate", src_arg, target], integrate_cb)
                        except Exception as e2:
                            raise e2
                    else:
                        raise e
                integrate_cb("__close__", "")
                # Try to parse PID from first lines and store
                try:
                    data_dir = os.path.join(self._data_dir, "artifacts")
                    job_dir = os.path.join(data_dir, str(job["id"]))
                    p = os.path.join(job_dir, "integrate_out.log")
                    pid: int | None = None
                    with open(p, "r", encoding="utf-8") as f:
                        for _ in range(5):
                            line = f.readline()
                            if not line:
                                break
                            if line.startswith("PID:"):
                                token = line.strip().split(":",1)[1].strip()
                                if token.isdigit():
                                    pid = int(token)
                                    break
                    if pid:
                        job.setdefault("pids", {})["integrate"] = pid
                        self._save(job)
                except Exception:
                    pass
                if code != 0:
                    self._append_log(job, f"p4 integrate exit={code}")
            else:
                # No valid integration parameters provided
                self._append_log(job, f"WARNING: No valid integration parameters found")
                self._append_log(job, f"  - branch_spec: '{branch_spec}'")
                self._append_log(job, f"  - source: '{source}'")
                self._append_log(job, f"  - target: '{target}'")
                self._append_log(job, f"Skipping integration step - proceeding to resolve phase")

            # Check if job was cancelled before resolve
            if self._check_job_cancelled(job):
                return
                
            # Auto-resolve two passes (-am) immediately after integrate
            self._set_stage(job, "resolving")
            # Simplified: only track the final resolve result
            resolve_output = []
            last_remaining = []  # Track result from Pass 2 to reuse
            
            for p in range(1, 3):
                # Check if job was cancelled during resolve passes
                if self._check_job_cancelled(job):
                    return
                    
                self._append_log(job, f"Auto-merge pass {p} (-am)")
                self._append_log(job, f"About to run: p4 resolve -am (pass {p})")
                
                # For the final pass, capture output; for earlier passes, just log
                if p == 2:
                    # streaming resolve output into resolve_current.log for final pass only
                    def _emit_resolve_final(text: str) -> None:
                        try:
                            # capture PID early when it appears
                            if text.startswith("PID:"):
                                token = text.strip().split(":",1)[1].strip()
                                if token.isdigit():
                                    pid_val = int(token)
                                    # Store pass-specific and canonical resolve PID
                                    job.setdefault("pids", {})["resolve_am2"] = pid_val
                                    job.setdefault("pids", {})["resolve"] = pid_val
                                    self._save(job)
                            _emit(job, "log", {"message": text})
                            resolve_output.append(text)
                        except Exception:
                            pass
                    file_cb = self._stream_log_artifact(job, "resolve_current", _emit_resolve_final)
                    on_chunk = file_cb  # two-arg (tag, data) callback
                else:
                    # For non-final passes, save to resolve_pass1.log and emit to events
                    def _emit_resolve_temp(text: str) -> None:
                        try:
                            if text.startswith("PID:"):
                                token = text.strip().split(":",1)[1].strip()
                                if token.isdigit():
                                    pid_val = int(token)
                                    # Store pass-specific and canonical resolve PID
                                    job.setdefault("pids", {})["resolve_am1"] = pid_val
                                    job.setdefault("pids", {})["resolve"] = pid_val
                                    self._save(job)
                            _emit(job, "log", {"message": text})
                        except Exception:
                            pass
                    # Create file-based callback for first pass too
                    file_cb_temp = self._stream_log_artifact(job, f"resolve_pass{p}", _emit_resolve_temp)
                    on_chunk = file_cb_temp  # two-arg (tag, data) callback
                try:
                    # Log resolve command
                    self._append_log(job, f"CMD(resolve): p4 resolve -am (pass {p})")
                    self._append_log(job, f"Executing resolve -am command (pass {p})...")
                    code = self._p4.run_p4_stream(["resolve", "-am"], on_chunk)
                    self._append_log(job, f"Resolve -am command completed with exit code: {code} (pass {p})")
                except Exception as e:
                    if "SSH session not active" in str(e):
                        self._append_log(job, "SSH dropped during resolve; reconnecting and retrying once")
                        try:
                            if hasattr(self._p4, 'runner') and hasattr(self._p4.runner, '_client'):
                                self._p4.runner._client = None  # type: ignore[attr-defined]
                        except Exception:
                            pass
                        code = self._p4.run_p4_stream(["resolve", "-am"], on_chunk)
                    else:
                        raise e
                
                # Close file-based callbacks for both passes
                if p == 2:
                    file_cb("__close__", "")
                else:
                    file_cb_temp("__close__", "")
                # No need for additional file copying - resolve_current.log is already saved
                # After pass, recompute remaining via preview (default CL)
                try:
                    self._append_log(job, f"Checking remaining conflicts after pass {p}...")
                    remaining, _ = self._p4.resolve_preview_default()
                    last_remaining = remaining  # Save for reuse
                    self._append_log(job, f"Pass {p} completed. {len(remaining)} conflicts remaining")
                except Exception as e:
                    self._append_log(job, f"ERROR: Post-resolve preview failed for pass {p}: {e}")
                    remaining = []  # Assume no remaining to continue
                    last_remaining = remaining
                # Do not break early; always perform two passes as requested

            # Preview resolves - reuse Pass 2 result to avoid redundant call
            self._set_stage(job, "resolve")
            self._append_log(job, "Final conflict check (reusing Pass 2 result)")
            try:
                # Reuse the result from Pass 2 instead of calling again
                conflicts = last_remaining
                self._append_log(job, f"Final conflict check: {len(conflicts)} file(s) need manual resolution")
                job["conflicts"] = conflicts
                # Persist conflict list for UI access
                if conflicts:
                    conflicts_text = "\n".join(conflicts)
                    self._persist_log_artifacts(job, "conflicts", conflicts_text)
                    self._persist_log_artifacts(job, "resolve_preview", f"Conflicts remaining after Pass 2:\n{conflicts_text}")
                else:
                    self._persist_log_artifacts(job, "resolve_preview", "No file(s) to resolve.")
                self._save(job)
            except Exception as e:
                self._append_log(job, f"ERROR: Conflict check failed: {e}")
                # Set error state and save
                job["status"] = "error"
                self._set_stage(job, "error")
                self._save(job)
                raise
            if conflicts:
                job["status"] = "needs_resolve"
                self._append_log(job, f"Needs resolve: {len(conflicts)} file(s)")
                # Persist conflict list
                self._persist_log_artifacts(job, "conflicts", "\n".join(conflicts))
                return

            # Determine flags (only trial is supported)
            job_trial = bool(payload.get("trial")) or bool(self._config.get("submit", {}).get("trial", False))

            # Check if job was cancelled before pre-submit checks
            if self._check_job_cancelled(job):
                return
                
            # Pre-submit checks (always run - no bypass option)
            self._set_stage(job, "pre_submit_checks")
            job["status"] = "running"
            self._save(job)
            self._append_log(job, "Pre-submit checks")
            if not self._pre_submit_checks(job):
                self._set_stage(job, "blocked")
                job["status"] = "blocked"
                self._save(job)
                return

            # If approval required, pause before submit
            # Simplified: no approval gate

            # Check if job was cancelled before submit
            if self._check_job_cancelled(job):
                return
                
            # Auto-shelve + p4push flow (no admin required)
            # Stage进入具体子阶段，status 保持 running
            self._set_stage(job, "shelve")
            job["status"] = "running"
            self._save(job)
            self._append_log(job, "Shelving and pushing changelist")
            # Collect opened files
            opened_files, opened_log = self._p4.opened_in_default()
            if opened_log:
                self._persist_log_artifacts(job, "opened", opened_log)
            # Create CL now for shelving stage
            src = source or job.get("payload", {}).get("source", "")
            tgt = target or job.get("payload", {}).get("target", "")
            src_cl = source_rev_change or job.get("payload", {}).get("changelist")
            spec_name_for_desc = str(job.get("payload", {}).get("spec", "")).strip()
            
            # Build properly formatted changelist description (all lines must start with tab)
            lines = []
            lines.append("\tREVIEW_INTEGRATE")
            lines.append("\t")  # Empty line
            
            # [INFRAFIX] line with actual CL number
            if src and src_cl:
                lines.append(f"\t[INFRAFIX] Mass integration from {src} @{src_cl}")
            elif src:
                lines.append(f"\t[INFRAFIX] Mass integration from {src} @head")
            else:
                lines.append("\t[INFRAFIX] Mass integration")
            
            # Spec information
            if spec_name_for_desc:
                lines.append(f"\tSPEC: {spec_name_for_desc}")
            
            # Target information
            if tgt:
                lines.append(f"\tTarget: {tgt}")
            
            templ = "\n".join(lines)
            # Log CL creation intent
            try:
                cl_display = f"@{src_cl}" if src_cl else "@head"
                self._append_log(job, f"CMD(change): p4 change -i (description: {src} {cl_display})")
            except Exception:
                pass
            cl = self._p4.create_changelist(templ)
            job["changelist"] = cl
            self._save(job)
            # Reopen files into this CL and shelve
            # Log reopen command (summary only)
            try:
                self._append_log(job, f"CMD(reopen): p4 reopen -c {cl} <opened_files>")
            except Exception:
                pass
            self._p4.reopen_to_changelist(cl, opened_files)
            # Try first-time shelve, then retry with -r to update if needed
            try:
                # Use stream methods for PID tracking
                s_out1, s_err1 = "", ""
                s_out2, s_err2 = "", ""
                
                def shelve_cb(tag: str, data: str):
                    nonlocal s_out1, s_err1, s_out2, s_err2
                    if "PID:" in data:
                        # Extract PID from log output
                        import re
                        pid_match = re.search(r'PID:\s*(\d+)', data)
                        if pid_match:
                            token = pid_match.group(1)
                            try:
                                job.setdefault("pids", {})["shelve"] = int(token)
                                self._save(job)
                            except Exception:
                                pass
                    _emit(job, "log", {"message": data})
                    if tag == "stdout":
                        s_out1 += data
                        s_out2 += data
                    else:
                        s_err1 += data
                        s_err2 += data
                
                self._append_log(job, "Running shelve operations...")
                # First shelve attempt
                try:
                    self._append_log(job, f"CMD(shelve): p4 shelve -f -c {cl}")
                except Exception:
                    pass
                try:
                    code1 = self._p4.run_p4_stream(["shelve", "-f", "-c", str(cl)], shelve_cb)
                except Exception as e:
                    # Normalize into code1 != 0 path below by simulating failure text
                    s_err1 += str(e)
                    code1 = -1
                if code1 != 0 and "no files to shelve" not in (s_out1.lower() + s_err1.lower()):
                        # If first shelve fails, try with -r flag
                        s_out2, s_err2 = "", ""
                        self._set_stage(job, "reshelve")
                        self._save(job)
                        try:
                            self._append_log(job, f"CMD(reshelve): p4 shelve -r -c {cl}")
                        except Exception:
                            pass
                        code2 = self._p4.run_p4_stream(["shelve", "-r", "-c", str(cl)], shelve_cb)
                        if code2 != 0 and "no files to shelve" not in (s_out2.lower() + s_err2.lower()):
                            raise P4Error(f"p4 shelve failed: {s_err2 or s_out2}")
            except Exception as e:
                if "no files to shelve" not in str(e).lower():
                    raise
            try:
                text = "\n".join([t for t in [(s_out1 or "") + ("\n" + s_err1 if s_err1 else ""), (s_out2 or "") + ("\n" + s_err2 if s_err2 else "")] if t.strip()])
                self._persist_log_artifacts(job, "shelve_out", text)
            except Exception:
                pass
            try:
                self._append_log(job, "Checking for name_check issues and remediating if needed")
                self._name_check_remediate(job, cl)
            except Exception:
                pass
            # Ensure a shelf exists before attempting p4push
            try:
                if not self._p4.shelf_exists(cl):
                    self._append_log(job, f"CL {cl} not shelved; forcing reshelve (-r)")
                    try:
                        _o, _e = "", ""
                        
                        def reshelve_cb(tag: str, data: str):
                            nonlocal _o, _e
                            if "PID:" in data:
                                # Extract PID from log output
                                import re
                                pid_match = re.search(r'PID:\s*(\d+)', data)
                                if pid_match:
                                    token = pid_match.group(1)
                                    try:
                                        job.setdefault("pids", {})["reshelve"] = int(token)
                                        self._save(job)
                                    except Exception:
                                        pass
                            _emit(job, "log", {"message": data})
                            if tag == "stdout":
                                _o += data
                            else:
                                _e += data
                        
                        self._set_stage(job, "reshelve")
                        self._save(job)
                        try:
                            self._append_log(job, f"CMD(reshelve): p4 shelve -r -c {cl}")
                        except Exception:
                            pass
                        code = self._p4.run_p4_stream(["shelve", "-r", "-c", str(cl)], reshelve_cb)
                        if code != 0 and "no files to shelve" not in (_o.lower() + _e.lower()):
                            self._append_log(job, f"Warning: reshelve returned code {code}")
                        
                        try:
                            self._persist_log_artifacts(job, "shelve_out", (_o or "") + (("\n" + _e) if _e else ""))
                        except Exception:
                            pass
                    except Exception:
                        pass
                if not self._p4.shelf_exists(cl):
                    self._append_log(job, f"* ERROR:  CL {cl} is not shelved after retries. Aborting submit.")
                    self._set_stage(job, "error")
                    job["status"] = "error"
                    self._save(job)
                    return
            except Exception:
                # Best-effort guard; if anything goes wrong here, fall through to p4push which will also surface errors
                pass
            # p4push (trial or real based on config)
            self._set_stage(job, "p4push")
            self._save(job)
            # streaming p4push output
            def _emit_push(text: str) -> None:
                try:
                    if text.startswith("PID:"):
                        token = text.strip().split(":",1)[1].strip()
                        if token.isdigit():
                            job.setdefault("pids", {})["p4push"] = int(token)
                            self._save(job)
                    _emit(job, "log", {"message": text})
                except Exception:
                    pass
            p4push_cb = self._stream_log_artifact(job, "p4push_out", _emit_push)
            try:
                # Log p4push command (summary)
                self._append_log(job, f"CMD(p4push): p4push {'-trial ' if job_trial else ''}-c {cl}")
                code = self._p4.p4push_stream(cl, job_trial, p4push_cb)
            except Exception as e:
                if "SSH session not active" in str(e):
                    self._append_log(job, "SSH dropped during p4push; reconnecting and retrying once")
                    try:
                        if hasattr(self._p4, 'runner') and hasattr(self._p4.runner, '_client'):
                            self._p4.runner._client = None  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    code = self._p4.p4push_stream(cl, job_trial, p4push_cb)
                else:
                    raise e
            p4push_cb("__close__", "")
            try:
                data_dir = os.path.join(self._data_dir, "artifacts")
                job_dir = os.path.join(data_dir, str(job["id"]))
                p = os.path.join(job_dir, "p4push_out.log")
                pid: int | None = None
                with open(p, "r", encoding="utf-8") as f:
                    for _ in range(5):
                        line = f.readline()
                        if not line:
                            break
                        if line.startswith("PID:"):
                            token = line.strip().split(":",1)[1].strip()
                            if token.isdigit():
                                pid = int(token)
                                break
                if pid:
                    job.setdefault("pids", {})["p4push"] = pid
                    self._save(job)
            except Exception:
                pass
            job["status"] = "pushed" if not job_trial else "pushed_trial"
            self._set_stage(job, "pushed")
            self._save(job)
            try:
                _emit(job, "status", {"status": job["status"]})
            except Exception:
                pass
        except P4Error as e:
            self._append_log(job, f"P4 error: {e}")
            self._set_stage(job, "error")
            job["status"] = "error"
            self._save(job)
            # Auto-cleanup on error if configured
            self._auto_cleanup_on_error(job)
            try:
                _emit(job, "status", {"status": "error"})
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            self._append_log(job, f"Unexpected error: {e}")
            self._set_stage(job, "error")
            job["status"] = "error"
            self._save(job)
            # Auto-cleanup on error if configured
            self._auto_cleanup_on_error(job)
            try:
                _emit(job, "status", {"status": "error"})
            except Exception:
                pass
    
    def _auto_cleanup_on_error(self, job: Job) -> None:
        """Automatically cleanup workspace on job error if configured."""
        try:
            auto_cleanup = bool(self._config.get("auto_cleanup_on_error", True))
            if not auto_cleanup:
                return
            
            self._append_log(job, "Auto-cleanup on error: reverting files and deleting changelist/shelf")
            
            # Revert any opened files in default changelist
            try:
                files, _ = self._p4.opened_in_default()
                if files:
                    out, err = self._p4.revert_files(files)
                    self._append_log(job, f"Reverted {len(files)} files")
            except Exception as e:
                self._append_log(job, f"Failed to revert files: {e}")
            
            # Delete shelf and changelist if present
            cl = job.get("changelist")
            if cl:
                try:
                    # Delete shelf first
                    s_out, s_err = self._p4.shelve_delete(int(cl))
                    if s_out or s_err:
                        self._append_log(job, f"Deleted shelf for CL {cl}")
                except Exception as e:
                    self._append_log(job, f"Failed to delete shelf: {e}")
                
                try:
                    # Delete changelist
                    c_out, c_err = self._p4.change_delete(int(cl))
                    if c_out or c_err:
                        self._append_log(job, f"Deleted changelist {cl}")
                except Exception as e:
                    self._append_log(job, f"Failed to delete changelist: {e}")
        except Exception as e:
            self._append_log(job, f"Auto-cleanup failed: {e}")

    def _pre_submit_checks(self, job: Job) -> bool:
        changelist = int(job.get("changelist") or 0)
        from fnmatch import fnmatch

        blocklist = self._config.get("blocklist", [])
        offending: List[str] = []
        opened = []
        try:
            # Always use default changelist for opened files check
            files, opened_log = self._p4.opened_in_default()
            opened = files
            if opened_log:
                self._persist_log_artifacts(job, "opened", opened_log)
        except Exception as e:  # noqa: BLE001
            self._append_log(job, f"opened query failed: {e}")

        for f in opened:
            for pattern in blocklist:
                if isinstance(pattern, str) and (fnmatch(f, pattern) or f == pattern):
                    offending.append(f)
                    break
        job["blocked_files"] = offending
        self._save(job)
        if offending:
            self._append_log(job, f"Blocked by patterns: {len(offending)} file(s)")
            return False

        hook = self._config.get("test_hook", {})
        if isinstance(hook, dict) and hook.get("command"):
            import subprocess, os

            cmd = [str(hook.get("command"))] + [str(a) for a in hook.get("args", [])]
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=self._p4.workspace_root,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self._append_log(job, f"Test stdout:\n{proc.stdout}")
                if proc.returncode != 0:
                    self._append_log(job, f"Test failed: {proc.stderr}")
                    return False
            except Exception as e:  # noqa: BLE001
                self._append_log(job, f"Test hook error: {e}")
                return False

        return True

    def preview_diff(self, job_id: str, file_path: str, mode: str = "merge") -> str:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("job not found")
        cl = job.get("changelist")
        return self._p4.preview_merge(file_path, int(cl) if cl else None, mode)

    def read_workspace_file(self, rel_or_abs_path: str) -> str:
        # Accept absolute workspace path or relative to workspace root
        path = rel_or_abs_path if rel_or_abs_path.startswith("/") else os.path.join(self._p4.workspace_root, rel_or_abs_path)
        return self._p4.read_file_text(path)

    def admin_submit(self, job_id: str) -> Job:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("job not found")
        # If job is awaiting_approval, flip to proceed
        if str(job.get("status")) == "awaiting_approval":
            job["status"] = "ready_to_submit"
            self._append_log(job, "Approval granted by admin")
            self._save(job)
        
        if not self._pre_submit_checks(job):
            job["status"] = "blocked"
            self._save(job)
            return job
        # Direct submit (alternative to p4push flow)
        try:
            if job.get("changelist"):
                out, err = self._p4.submit(int(job["changelist"]))
            else:
                out, err = self._p4.submit_default(job.get("payload", {}).get("description", "Submit"))
            self._append_log(job, f"Submit output: {out}")
            if err:
                self._append_log(job, f"Submit stderr: {err}")
        except Exception as e:
            self._append_log(job, f"Submit failed: {e}")
            job["status"] = "error"
            self._save(job)
            return job
        job["status"] = "pushed"
        self._save(job)
        return job

    def cancel_job(self, job_id: str) -> Job:
        lock = self._get_job_lock(job_id)
        with lock:
            job = self.get_job(job_id)
            if not job:
                raise ValueError("job not found")
            if str(job.get("status")) in {"submitted", "error", "cancelled", "pushed", "pushed_trial"}:
                return job
            
            # Kill any running processes before marking as cancelled
            killed_pids = []
            pids = job.get("pids", {}) if isinstance(job.get("pids", {}), dict) else {}
            for stage, pid in pids.items():
                if isinstance(pid, int) and pid > 0:
                    try:
                        # Check if process is still alive
                        if self._p4.check_pid(pid):
                            # Try to kill the process (SIGTERM first)
                            if self._p4.kill_pid(pid, signal=15):  # SIGTERM
                                killed_pids.append(f"{stage}={pid}")
                                self._append_log(job, f"Terminated {stage} process (PID {pid})")
                                # Verify kill succeeded
                                time.sleep(0.5)
                                if self._p4.check_pid(pid):
                                    # Process still alive, try SIGKILL
                                    if self._p4.kill_pid(pid, signal=9):  # SIGKILL
                                        self._append_log(job, f"Force-killed {stage} process (PID {pid})")
                                    else:
                                        self._append_log(job, f"WARNING: Failed to force-kill {stage} process (PID {pid})")
                            else:
                                self._append_log(job, f"Failed to kill {stage} process (PID {pid})")
                        else:
                            self._append_log(job, f"Process {stage} (PID {pid}) already terminated")
                    except Exception as e:
                        self._append_log(job, f"Error killing {stage} process (PID {pid}): {str(e)}")
            
            job["status"] = "cancelled"
            self._cancelled_ids.add(job_id)
            self._manual_resolve_jobs.discard(job_id)
            
            if killed_pids:
                self._append_log(job, f"Job cancelled - killed processes: {', '.join(killed_pids)}")
            else:
                self._append_log(job, "Job marked as cancelled")
            
            self._save(job)
            # Cleanup will happen when job reaches terminal state
            return job

    def admin_cancel(self, job_id: str) -> Job:
        return self.cancel_job(job_id)

    # Approval flow removed
    
    def _continue_to_submit(self, job: Job) -> None:
        """Continue from resolved state to submit. Used by auto-resolve after conflicts clear.
        This is the submission flow extracted from rescan_conflicts without the auto-submit behavior.
        """
        job_id = str(job["id"])
        try:
            # Run pre-submit checks
            if not self._pre_submit_checks(job):
                job["status"] = "blocked"
                self._set_stage(job, "blocked")
                self._save(job)
                return
            
            # Auto-shelve + push
            self._set_stage(job, "submit")
            job["status"] = "submit"
            self._save(job)
            self._append_log(job, "No conflicts; auto-shelving and pushing changelist")
            
            # Get opened files
            try:
                opened_files, opened_log = self._p4.opened_in_default()
                if opened_log:
                    self._persist_log_artifacts(job, "opened", opened_log)
            except Exception as e:  # noqa: BLE001
                opened_files = []
                self._append_log(job, f"opened query failed: {e}")
            
            # Create changelist
            src = str(job.get("payload", {}).get("source", ""))
            tgt = str(job.get("payload", {}).get("target", ""))
            src_cl = job.get("payload", {}).get("changelist")
            spec_name_for_desc = str(job.get("payload", {}).get("spec", "")).strip()
            
            # Build properly formatted changelist description
            lines = []
            lines.append("\tREVIEW_INTEGRATE")
            lines.append("\t")
            
            if src and src_cl:
                lines.append(f"\t[INFRAFIX] Mass integration from {src} @{src_cl}")
            elif src:
                lines.append(f"\t[INFRAFIX] Mass integration from {src} @head")
            else:
                lines.append("\t[INFRAFIX] Mass integration")
            
            if spec_name_for_desc:
                lines.append(f"\tSPEC: {spec_name_for_desc}")
            
            if tgt:
                lines.append(f"\tTarget: {tgt}")
            
            templ = "\n".join(lines)
            cl = self._p4.create_changelist(templ)
            job["changelist"] = cl
            self._save(job)
            
            if opened_files:
                self._p4.reopen_to_changelist(cl, opened_files)
            
            # Shelve with name_check handling
            self._shelve_with_name_check_simple(job, cl)
            
            # Ensure shelf exists before pushing
            try:
                if not self._p4.shelf_exists(cl):
                    self._append_log(job, f"* ERROR: CL {cl} is not shelved after retries. Aborting submit.")
                    self._set_stage(job, "error")
                    job["status"] = "error"
                    self._save(job)
                    return
            except Exception:
                pass
            
            # Proceed to push
            trial = bool(self._config.get("submit", {}).get("trial", False))
            p_out, p_err = self._p4.p4push(cl, trial)
            try:
                self._persist_log_artifacts(job, "p4push_out", (p_out or "") + ("\n" + p_err if p_err else ""))
            except Exception:
                pass
            
            job["status"] = "submitted" if not trial else "pushed_trial"
            self._set_stage(job, "submitted")
            self._save(job)
        except Exception as e:  # noqa: BLE001
            self._append_log(job, f"Continue to submit failed: {e}")
            job["status"] = "error"
            self._set_stage(job, "error")
            self._save(job)

    def mark_resolving_gui(self, job_id: str, on: bool = True) -> Job:
        lock = self._get_job_lock(job_id)
        with lock:
            job = self.get_job(job_id)
            if not job:
                raise ValueError("job not found")
            # Track manual resolve state
            if on:
                self._manual_resolve_jobs.add(job_id)
                self._set_stage(job, "resolving")
            else:
                self._manual_resolve_jobs.discard(job_id)
                # fall back to resolve stage so UI may rescan
                self._set_stage(job, "resolve")
            self._save(job)
            return job

    def rescan_conflicts(self, job_id: str) -> Job:
        """Manual-only rescan of conflicts. Does NOT auto-submit even if clean.
        Use continue_to_submit() API to proceed after conflicts are cleared.
        """
        lock = self._get_job_lock(job_id)
        with lock:
            job = self.get_job(job_id)
            if not job:
                raise ValueError("job not found")
            
            # Check for debounce: if called within 10 seconds, use cached result
            last_scan = job.get('last_manual_scan', 0)
            now = time.time()
            if now - last_scan < 10:
                self._append_log(job, "Rescan debounced (called within 10s), using cached result")
                cached = self._conflict_cache.get(job_id)
                if cached:
                    _, cached_conflicts = cached
                    self._append_log(job, f"Using cached conflicts: {len(cached_conflicts)} file(s)")
                    return job
            
            job['last_manual_scan'] = now
            self._save(job)
            self._append_log(job, "Rescanning conflicts via p4 resolve -n (manual rescan)")
            try:
                # Use explicit shell-based preview to guarantee env/bootenv
                conflicts, rp_log = self._p4.resolve_preview_shell()  # type: ignore[attr-defined]
                # Update cache
                self._conflict_cache[job_id] = (now, conflicts)
                # Replace conflicts list atomically with fresh scan
                job["conflicts"] = list(conflicts)
                if rp_log:
                    try:
                        self._persist_log_artifacts(job, "resolve_preview", rp_log)
                    except Exception:
                        pass
                try:
                    self._persist_log_artifacts(job, "conflicts", "\n".join(job["conflicts"]))
                except Exception:
                    pass
                # Update status/stage - MANUAL ONLY, no auto-submit
                if job["conflicts"]:
                    job["status"] = "needs_resolve"
                    self._set_stage(job, "resolve")
                    self._save(job)
                else:
                    # Conflicts cleared but DO NOT auto-submit in manual flow
                    job["status"] = "ready_to_submit"
                    self._set_stage(job, "ready_to_submit")
                    self._append_log(job, "Conflicts cleared. Ready to submit. Use continue_to_submit API or submit button.")
                    self._save(job)
                return job
            except Exception as e:  # noqa: BLE001
                # Fallback: mark as error and return
                self._append_log(job, f"rescan failed: {e}")
                job["status"] = "error"
                self._set_stage(job, "error")
                self._save(job)
                return job
    
    def continue_to_submit(self, job_id: str) -> Job:
        """Explicit API to continue from ready_to_submit to actual submission.
        Called by UI after manual rescan shows clean, or by auto-resolve when conflicts clear.
        """
        lock = self._get_job_lock(job_id)
        with lock:
            job = self.get_job(job_id)
            if not job:
                raise ValueError("job not found")
            
            # Only proceed if job is in ready_to_submit state
            if job.get("status") != "ready_to_submit":
                self._append_log(job, f"Cannot continue to submit from status: {job.get('status')}")
                return job
            
            self._append_log(job, "Continuing to submit after conflicts cleared")
            self._continue_to_submit(job)
            return job

    def check_resolve_preview_log(self, job_id: str) -> Job:
        job = self.get_job(job_id)
        if not job:
            raise ValueError("job not found")
        # Minimal logic: only check if resolve_preview.log contains "No file(s) to resolve.".
        # If yes -> run pre-submit checks and move to ready_to_submit or blocked.
        # If not -> ensure we stay in resolve stage.
        data_dir = os.path.join(self._data_dir, "artifacts")
        job_dir = os.path.join(data_dir, str(job["id"]))
        path = os.path.join(job_dir, "resolve_preview.log")
        if not os.path.exists(path):
            # Quietly return if preview not available
            self._save(job)
            return job
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception as e:  # noqa: BLE001
            self._append_log(job, f"failed reading resolve_preview.log: {e}")
            self._save(job)
            return job
        if "No file(s) to resolve." in (text or ""):
            # Clear conflicts and advance
            job["conflicts"] = []
            if self._pre_submit_checks(job):
                job["status"] = "ready_to_submit"
                self._set_stage(job, "ready_to_submit")
                self._append_log(job, "Pre-submit checks passed, ready for submission")
            else:
                job["status"] = "blocked"
                self._set_stage(job, "blocked")
            self._save(job)
            return job
        # Otherwise remain in resolve
        job["status"] = "needs_resolve"
        self._set_stage(job, "resolve")
        self._save(job)
        return job
