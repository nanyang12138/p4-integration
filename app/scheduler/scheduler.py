"""
Scheduler - APScheduler-based Job Scheduling

Handles cron-based scheduling of job templates.
"""
import os
import json
import uuid
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.memory import MemoryJobStore

from app.config import get_workspace_data_dir

logger = logging.getLogger("Scheduler")


class ScheduleManager:
    """Manages scheduled jobs using APScheduler.
    
    Schedules are stored per-workspace in {workspace}/.p4_integ/schedules.json
    """
    
    def __init__(self):
        self._scheduler = BackgroundScheduler(
            jobstores={'default': MemoryJobStore()},
            job_defaults={
                'coalesce': True,
                'max_instances': 1,
                'misfire_grace_time': 3600  # 1 hour grace time for misfires
            }
        )
        self._schedules: Dict[str, dict] = {}  # schedule_id -> schedule_data
        self._job_runner: Optional[Callable] = None
        self._started = False
    
    def set_job_runner(self, runner: Callable[[str, str, dict], None]):
        """Set the callback for running scheduled jobs.
        
        Callback signature: runner(schedule_id: str, template_id: str, config: dict)
        """
        self._job_runner = runner
    
    def start(self):
        """Start the scheduler."""
        if not self._started:
            self._scheduler.start()
            self._started = True
            logger.info("Scheduler started")
    
    def shutdown(self):
        """Shutdown the scheduler."""
        if self._started:
            self._scheduler.shutdown(wait=False)
            self._started = False
            logger.info("Scheduler shutdown")
    
    def _get_schedules_file(self, workspace: str) -> str:
        """Get path to schedules.json for a workspace."""
        data_dir = get_workspace_data_dir(workspace)
        return os.path.join(data_dir, "schedules.json")
    
    def _load_schedules(self, workspace: str) -> Dict[str, dict]:
        """Load schedules from workspace file."""
        filepath = self._get_schedules_file(workspace)
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load schedules from {filepath}: {e}")
        return {}
    
    def _save_schedules(self, workspace: str, schedules: Dict[str, dict]):
        """Save schedules to workspace file."""
        filepath = self._get_schedules_file(workspace)
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(schedules, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save schedules to {filepath}: {e}")
    
    def _execute_schedule(self, schedule_id: str, force: bool = False):
        """Execute a scheduled job.
        
        Args:
            schedule_id: ID of the schedule to execute
            force: If True, execute even if disabled (used by Run Now)
        """
        schedule = self._schedules.get(schedule_id)
        if not schedule:
            logger.warning(f"Schedule {schedule_id} not found, skipping execution")
            return
        
        if not force and not schedule.get('enabled', True):
            logger.info(f"Schedule {schedule_id} is disabled, skipping")
            return
        
        logger.info(f"Executing schedule {schedule_id} ({schedule.get('name', 'unnamed')})")
        
        # Update last_run
        schedule['last_run'] = datetime.now().isoformat()
        schedule['last_status'] = 'running'
        
        job_id = None
        try:
            if self._job_runner:
                job_id = self._job_runner(
                    schedule_id,
                    schedule.get('template_id'),
                    schedule.get('config', {})
                )
                schedule['last_status'] = 'success'
                schedule['last_job_id'] = job_id
            else:
                logger.error("No job runner configured")
                schedule['last_status'] = 'failed'
                schedule['last_error'] = 'No job runner configured'
        except Exception as e:
            logger.error(f"Schedule {schedule_id} execution failed: {e}")
            schedule['last_status'] = 'failed'
            schedule['last_error'] = str(e)
        
        # Save updated schedule
        workspace = schedule.get('workspace')
        if workspace:
            schedules = self._load_schedules(workspace)
            schedules[schedule_id] = schedule
            self._save_schedules(workspace, schedules)
    
    def create_schedule(
        self,
        name: str,
        template_id: str,
        cron: str,
        owner: str,
        workspace: str,
        config: Optional[Dict[str, Any]] = None,
        enabled: bool = True
    ) -> Optional[dict]:
        """Create a new schedule.
        
        Args:
            name: Schedule name
            template_id: ID of the template to run
            cron: Cron expression (e.g., "0 2 * * *" for 2am daily)
            owner: Username of the schedule creator
            workspace: Workspace path (determines storage location)
            config: Optional config overrides for the template
            enabled: Whether the schedule is enabled
            
        Returns:
            Created schedule dict or None on failure
        """
        schedule_id = f"sch-{uuid.uuid4().hex[:12]}"
        
        try:
            # Parse and validate cron expression
            trigger = CronTrigger.from_crontab(cron)
        except Exception as e:
            logger.error(f"Invalid cron expression '{cron}': {e}")
            return None
        
        schedule = {
            "id": schedule_id,
            "name": name,
            "template_id": template_id,
            "owner": owner,
            "workspace": workspace,
            "cron": cron,
            "config": config or {},
            "enabled": enabled,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "last_run": None,
            "last_status": None,
            "last_error": None
        }
        
        # Calculate next run time
        next_run = trigger.get_next_fire_time(None, datetime.now())
        schedule["next_run"] = next_run.isoformat() if next_run else None
        
        # Store in memory
        self._schedules[schedule_id] = schedule
        
        # Persist to file
        schedules = self._load_schedules(workspace)
        schedules[schedule_id] = schedule
        self._save_schedules(workspace, schedules)
        
        # Add to APScheduler if enabled
        if enabled:
            self._add_to_scheduler(schedule_id, cron)
        
        logger.info(f"Created schedule {schedule_id}: {name} (cron: {cron})")
        return schedule
    
    def _add_to_scheduler(self, schedule_id: str, cron: str):
        """Add a schedule to APScheduler."""
        try:
            trigger = CronTrigger.from_crontab(cron)
            self._scheduler.add_job(
                self._execute_schedule,
                trigger=trigger,
                args=[schedule_id],
                id=schedule_id,
                replace_existing=True
            )
            logger.info(f"Added schedule {schedule_id} to APScheduler")
        except Exception as e:
            logger.error(f"Failed to add schedule {schedule_id} to APScheduler: {e}")
    
    def _remove_from_scheduler(self, schedule_id: str):
        """Remove a schedule from APScheduler."""
        try:
            self._scheduler.remove_job(schedule_id)
            logger.info(f"Removed schedule {schedule_id} from APScheduler")
        except Exception as e:
            # Job might not exist
            pass
    
    def get_schedule(self, schedule_id: str) -> Optional[dict]:
        """Get a schedule by ID."""
        return self._schedules.get(schedule_id)
    
    def list_schedules(self, workspace: str = None, owner: str = None) -> List[dict]:
        """List schedules, optionally filtered by workspace or owner."""
        result = []
        
        for schedule in self._schedules.values():
            if workspace and schedule.get('workspace') != workspace:
                continue
            if owner and schedule.get('owner') != owner:
                continue
            
            # Update next_run from APScheduler
            job = self._scheduler.get_job(schedule['id'])
            if job and job.next_run_time:
                schedule['next_run'] = job.next_run_time.isoformat()
            
            result.append(schedule)
        
        # Sort by next_run
        result.sort(key=lambda s: s.get('next_run') or '', reverse=False)
        return result
    
    def load_workspace_schedules(self, workspace: str):
        """Load and activate schedules from a workspace file."""
        schedules = self._load_schedules(workspace)
        
        for schedule_id, schedule in schedules.items():
            self._schedules[schedule_id] = schedule
            
            if schedule.get('enabled', True):
                self._add_to_scheduler(schedule_id, schedule['cron'])
        
        logger.info(f"Loaded {len(schedules)} schedules from workspace {workspace}")
    
    def update_schedule(
        self,
        schedule_id: str,
        updates: Dict[str, Any]
    ) -> Optional[dict]:
        """Update an existing schedule.
        
        Args:
            schedule_id: Schedule ID
            updates: Dict with fields to update
        """
        schedule = self._schedules.get(schedule_id)
        if not schedule:
            return None
        
        # Apply updates
        if 'name' in updates:
            schedule['name'] = updates['name']
        if 'cron' in updates:
            # Validate new cron expression
            try:
                CronTrigger.from_crontab(updates['cron'])
                schedule['cron'] = updates['cron']
            except Exception as e:
                logger.error(f"Invalid cron expression: {e}")
                return None
        if 'config' in updates:
            schedule['config'] = updates['config']
        if 'enabled' in updates:
            schedule['enabled'] = updates['enabled']
        
        schedule['updated_at'] = datetime.now().isoformat()
        
        # Update next_run
        try:
            trigger = CronTrigger.from_crontab(schedule['cron'])
            next_run = trigger.get_next_fire_time(None, datetime.now())
            schedule['next_run'] = next_run.isoformat() if next_run else None
        except Exception:
            pass
        
        # Update APScheduler
        self._remove_from_scheduler(schedule_id)
        if schedule.get('enabled', True):
            self._add_to_scheduler(schedule_id, schedule['cron'])
        
        # Persist
        workspace = schedule.get('workspace')
        if workspace:
            schedules = self._load_schedules(workspace)
            schedules[schedule_id] = schedule
            self._save_schedules(workspace, schedules)
        
        logger.info(f"Updated schedule {schedule_id}")
        return schedule
    
    def delete_schedule(self, schedule_id: str) -> bool:
        """Delete a schedule."""
        schedule = self._schedules.get(schedule_id)
        if not schedule:
            return False
        
        # Remove from APScheduler
        self._remove_from_scheduler(schedule_id)
        
        # Remove from memory
        del self._schedules[schedule_id]
        
        # Remove from file
        workspace = schedule.get('workspace')
        if workspace:
            schedules = self._load_schedules(workspace)
            if schedule_id in schedules:
                del schedules[schedule_id]
                self._save_schedules(workspace, schedules)
        
        logger.info(f"Deleted schedule {schedule_id}")
        return True
    
    def enable_schedule(self, schedule_id: str) -> bool:
        """Enable a schedule."""
        return self.update_schedule(schedule_id, {'enabled': True}) is not None
    
    def disable_schedule(self, schedule_id: str) -> bool:
        """Disable a schedule."""
        return self.update_schedule(schedule_id, {'enabled': False}) is not None
    
    def run_now(self, schedule_id: str) -> Optional[str]:
        """Run a schedule immediately (manual trigger).
        
        Returns:
            job_id if successful, None if schedule not found or execution failed.
        """
        schedule = self._schedules.get(schedule_id)
        if not schedule:
            return None
        
        logger.info(f"Manual run triggered for schedule {schedule_id}")
        self._execute_schedule(schedule_id, force=True)
        return schedule.get('last_job_id')


# Global instance
scheduler_manager = ScheduleManager()

