"""
Workspace Queue Manager

Manages job queues per workspace to prevent concurrent operations on the same workspace.
When a workspace is busy, new jobs are queued and automatically started when the current job completes.
"""
import logging
import threading
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field

logger = logging.getLogger("WorkspaceQueue")


@dataclass
class QueuedJob:
    """A job waiting in the queue"""
    job_id: str
    owner: str
    template_id: Optional[str]
    spec: Dict[str, Any]
    queued_at: str
    
    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "owner": self.owner,
            "template_id": self.template_id,
            "queued_at": self.queued_at
        }


@dataclass
class WorkspaceState:
    """State of a workspace"""
    workspace: str
    running_job_id: Optional[str] = None
    running_job_owner: Optional[str] = None
    started_at: Optional[str] = None
    queued_jobs: List[QueuedJob] = field(default_factory=list)
    
    def is_busy(self) -> bool:
        return self.running_job_id is not None
    
    def queue_position(self, job_id: str) -> int:
        """Get position in queue (1-based), or 0 if not in queue"""
        for i, qj in enumerate(self.queued_jobs):
            if qj.job_id == job_id:
                return i + 1
        return 0
    
    def to_dict(self) -> dict:
        return {
            "workspace": self.workspace,
            "running_job_id": self.running_job_id,
            "running_job_owner": self.running_job_owner,
            "started_at": self.started_at,
            "queued_jobs": [qj.to_dict() for qj in self.queued_jobs],
            "queue_length": len(self.queued_jobs)
        }


class WorkspaceQueueManager:
    """Manages workspace-based job queues.
    
    Ensures only one job runs per workspace at a time.
    Jobs submitted to a busy workspace are queued and started automatically.
    """
    
    def __init__(self):
        self._workspaces: Dict[str, WorkspaceState] = {}
        self._lock = threading.RLock()
        self._on_job_ready_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None
    
    def set_job_ready_callback(self, callback: Callable[[str, Dict[str, Any]], None]):
        """Set callback to be called when a queued job is ready to run.
        
        Callback signature: callback(job_id: str, spec: dict)
        """
        self._on_job_ready_callback = callback
    
    def _normalize_workspace(self, workspace: str) -> str:
        """Normalize workspace path for consistent comparison"""
        if not workspace:
            return ""
        # Remove trailing slashes and normalize
        return workspace.rstrip('/\\').lower()
    
    def _get_or_create_state(self, workspace: str) -> WorkspaceState:
        """Get or create workspace state"""
        key = self._normalize_workspace(workspace)
        if key not in self._workspaces:
            self._workspaces[key] = WorkspaceState(workspace=workspace)
        return self._workspaces[key]
    
    def check_workspace(self, workspace: str) -> dict:
        """Check if a workspace is available or busy.
        
        Returns:
            dict with keys:
            - available: bool
            - running_job_id: str or None
            - running_job_owner: str or None
            - queue_length: int
        """
        with self._lock:
            state = self._get_or_create_state(workspace)
            return {
                "available": not state.is_busy(),
                "running_job_id": state.running_job_id,
                "running_job_owner": state.running_job_owner,
                "queue_length": len(state.queued_jobs)
            }
    
    def try_acquire(self, workspace: str, job_id: str, owner: str) -> dict:
        """Try to acquire a workspace for a job.
        
        If the workspace is available, acquires it immediately.
        If busy, the job is queued.
        
        Returns:
            dict with keys:
            - acquired: bool - True if job can run immediately
            - queued: bool - True if job was added to queue
            - position: int - Queue position (0 if acquired, 1+ if queued)
            - running_job_id: str or None - Current job if queued
            - running_job_owner: str or None
        """
        with self._lock:
            state = self._get_or_create_state(workspace)
            
            if not state.is_busy():
                # Workspace available - acquire it
                state.running_job_id = job_id
                state.running_job_owner = owner
                state.started_at = datetime.now().isoformat()
                logger.info(f"Workspace {workspace} acquired by job {job_id} (owner: {owner})")
                return {
                    "acquired": True,
                    "queued": False,
                    "position": 0,
                    "running_job_id": None,
                    "running_job_owner": None
                }
            else:
                # Workspace busy - queue the job
                # Note: spec will be set separately via queue_job
                logger.info(f"Workspace {workspace} busy (job {state.running_job_id}), job {job_id} will be queued")
                return {
                    "acquired": False,
                    "queued": False,  # Not queued yet, caller should call queue_job
                    "position": len(state.queued_jobs) + 1,
                    "running_job_id": state.running_job_id,
                    "running_job_owner": state.running_job_owner
                }
    
    def queue_job(
        self,
        workspace: str,
        job_id: str,
        owner: str,
        spec: Dict[str, Any],
        template_id: Optional[str] = None
    ) -> int:
        """Add a job to the workspace queue.
        
        Returns:
            Queue position (1-based)
        """
        with self._lock:
            state = self._get_or_create_state(workspace)
            
            queued_job = QueuedJob(
                job_id=job_id,
                owner=owner,
                template_id=template_id,
                spec=spec,
                queued_at=datetime.now().isoformat()
            )
            state.queued_jobs.append(queued_job)
            position = len(state.queued_jobs)
            
            logger.info(f"Job {job_id} queued for workspace {workspace} at position {position}")
            return position
    
    def release(self, workspace: str, job_id: str) -> Optional[QueuedJob]:
        """Release a workspace after job completion.
        
        If there are queued jobs, returns the next one to run.
        
        Args:
            workspace: Workspace path
            job_id: Job ID that is completing
            
        Returns:
            Next QueuedJob to run, or None if queue is empty
        """
        with self._lock:
            key = self._normalize_workspace(workspace)
            if key not in self._workspaces:
                return None
            
            state = self._workspaces[key]
            
            # Verify this is the running job
            if state.running_job_id != job_id:
                logger.warning(f"Job {job_id} trying to release workspace {workspace} but {state.running_job_id} is running")
                return None
            
            # Release the workspace
            state.running_job_id = None
            state.running_job_owner = None
            state.started_at = None
            
            logger.info(f"Workspace {workspace} released by job {job_id}")
            
            # Check if there's a queued job
            if state.queued_jobs:
                next_job = state.queued_jobs.pop(0)
                
                # Acquire for the next job
                state.running_job_id = next_job.job_id
                state.running_job_owner = next_job.owner
                state.started_at = datetime.now().isoformat()
                
                logger.info(f"Workspace {workspace} acquired by queued job {next_job.job_id}")
                
                # Trigger callback if set
                if self._on_job_ready_callback:
                    try:
                        self._on_job_ready_callback(next_job.job_id, next_job.spec)
                    except Exception as e:
                        logger.error(f"Job ready callback failed for {next_job.job_id}: {e}")
                
                return next_job
            
            return None
    
    def remove_from_queue(self, workspace: str, job_id: str) -> bool:
        """Remove a job from the queue (e.g., when cancelled).
        
        Returns:
            True if job was found and removed
        """
        with self._lock:
            key = self._normalize_workspace(workspace)
            if key not in self._workspaces:
                return False
            
            state = self._workspaces[key]
            
            for i, qj in enumerate(state.queued_jobs):
                if qj.job_id == job_id:
                    state.queued_jobs.pop(i)
                    logger.info(f"Job {job_id} removed from queue for workspace {workspace}")
                    return True
            
            return False
    
    def get_workspace_state(self, workspace: str) -> Optional[dict]:
        """Get the current state of a workspace."""
        with self._lock:
            key = self._normalize_workspace(workspace)
            if key in self._workspaces:
                return self._workspaces[key].to_dict()
            return None
    
    def get_all_states(self) -> List[dict]:
        """Get states of all workspaces with active jobs or queues."""
        with self._lock:
            return [
                state.to_dict() 
                for state in self._workspaces.values()
                if state.is_busy() or state.queued_jobs
            ]
    
    def get_user_queued_jobs(self, owner: str) -> List[dict]:
        """Get all queued jobs for a specific user across all workspaces."""
        with self._lock:
            result = []
            for state in self._workspaces.values():
                for qj in state.queued_jobs:
                    if qj.owner == owner:
                        result.append({
                            **qj.to_dict(),
                            "workspace": state.workspace,
                            "position": state.queue_position(qj.job_id)
                        })
            return result


# Global instance
workspace_queue = WorkspaceQueueManager()

