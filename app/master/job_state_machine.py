"""
JobStateMachine - P4 Integration Job State Machine
Handles event-driven state transitions based on Agent events.
"""
import asyncio
import logging
import uuid
import re
import os
import shlex
import json
from typing import Dict, Optional, List, Any, Callable
from datetime import datetime
from enum import Enum

# Configure logging
logger = logging.getLogger("JobStateMachine")

class Stage(Enum):
    """Job processing stages"""
    INIT = "INIT"
    GET_LATEST_CL = "GET_LATEST_CL"
    SYNC = "SYNC"
    INTEGRATE = "INTEGRATE"
    RESOLVE_PASS_1 = "RESOLVE_PASS_1"
    RESOLVE_CHECK = "RESOLVE_CHECK"
    NEEDS_RESOLVE = "NEEDS_RESOLVE"
    PRE_SUBMIT = "PRE_SUBMIT"
    SHELVE = "SHELVE"
    P4PUSH = "P4PUSH"
    CLEANUP = "CLEANUP"  # Reverts opened files on failure before entering ERROR
    DONE = "DONE"
    ERROR = "ERROR"

# Stages where P4 files may be opened and need revert on failure.
# Used by error handlers and cancel logic to decide if CLEANUP is needed.
_NEEDS_CLEANUP_STAGE_VALUES = {
    Stage.INTEGRATE.value, Stage.RESOLVE_PASS_1.value,
    Stage.RESOLVE_CHECK.value, Stage.NEEDS_RESOLVE.value,
    Stage.PRE_SUBMIT.value, Stage.SHELVE.value
}

class JobStateMachine:
    """Core business logic state machine"""
    def __init__(self, agent_server, config: dict):
        self.agent_server = agent_server
        self.config = config  # Store config for P4 client info
        self.jobs: Dict[str, dict] = {}  # job_id -> job_info
        self.cmd_to_job: Dict[str, str] = {}  # cmd_id -> job_id
        self.logs: Dict[str, List[dict]] = {}  # job_id -> [log_entries]
        
        # Register as event handler
        self.agent_server.register_event_handler(self)
        
        # Background task for conflict monitoring
        self.monitor_tasks: Dict[str, asyncio.Task] = {}
        
        # Workspace queue manager reference (set externally)
        self._workspace_queue = None
    
    def set_workspace_queue(self, queue_manager):
        """Set the workspace queue manager for job coordination"""
        self._workspace_queue = queue_manager
    
    def create_job(self, job_id: str, agent_id: str, spec: dict, owner: str = None) -> dict:
        """Initialize a new job
        
        Args:
            job_id: Unique job identifier
            agent_id: Connected agent identifier
            spec: Job specification
            owner: Owner username (p4_user). If None, extracted from spec.p4.user
        """
        # If changelist not specified, mark as latest
        if not spec.get('changelist'):
            spec['changelist_source'] = 'latest'
        else:
            spec['changelist_source'] = 'user_specified'
        
        # Determine owner - use provided value, or extract from p4 config
        if owner is None:
            owner = spec.get('p4', {}).get('user', 'unknown')
        
        job = {
            "job_id": job_id,
            "agent_id": agent_id,
            "owner": owner,  # Job owner for filtering
            "spec": spec,
            "stage": Stage.INIT.value,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "error": None,
            "current_cmd_id": None,
            "history": [],  # State transition history
            "changelist": None,  # Will be set during SHELVE stage
            "source_changelist": spec.get('changelist', 'latest')  # Source CL for integrate
        }
        self.jobs[job_id] = job
        self.logs[job_id] = []
        logger.info(f"Created job {job_id} for agent {agent_id}, owner: {owner}, source CL: {spec.get('changelist', 'latest')}")
        return job
    
    async def start_job(self, job_id: str):
        """Kick off the job"""
        if job_id not in self.jobs:
            raise ValueError(f"Job {job_id} not found")
        
        job = self.jobs[job_id]
        
        # Check if we need to get latest changelist
        if not job.get("source_changelist") or job["source_changelist"] == "latest":
            # Need to get latest CL first
            await self.transition_to(job_id, Stage.GET_LATEST_CL)
        else:
            # User specified CL, skip to SYNC
            await self.transition_to(job_id, Stage.SYNC)
    
    async def transition_to(self, job_id: str, next_stage: Stage):
        """Transition to next stage and execute corresponding command"""
        job = self.jobs[job_id]
        old_stage = job["stage"]
        job["stage"] = next_stage.value
        job["updated_at"] = datetime.now().isoformat()
        job["history"].append({
            "from": old_stage,
            "to": next_stage.value,
            "time": datetime.now().isoformat()
        })
        
        logger.info(f"Job {job_id}: {old_stage} -> {next_stage.value}")
        
        # Handle special monitoring for NEEDS_RESOLVE
        if old_stage == Stage.NEEDS_RESOLVE.value and next_stage != Stage.NEEDS_RESOLVE:
            self._stop_conflict_monitor(job_id)
        
        if next_stage == Stage.NEEDS_RESOLVE:
            self._start_conflict_monitor(job_id)
            logger.info(f"Job {job_id} waiting for manual conflict resolution")
            return

        if next_stage == Stage.DONE:
            logger.info(f"Job {job_id} completed successfully")
            await self._shutdown_agent(job_id, "Job completed successfully")
            self._release_workspace(job_id)
            return
            
        if next_stage == Stage.ERROR:
            logger.error(f"Job {job_id} failed")
            await self._shutdown_agent(job_id, "Job failed")
            self._release_workspace(job_id)
            return
        
        if next_stage == Stage.CLEANUP:
            logger.warning(f"Job {job_id} entering cleanup - reverting opened files")
            self._add_log_entry(job_id, "stderr", 
                "[CLEANUP] Job failed, reverting opened files in workspace...")
            command = self._get_stage_command(job, next_stage)
            if command:
                await self._execute_command(job_id, command)
            else:
                # Cannot build cleanup command (e.g. missing P4 config), go to ERROR directly
                logger.error(f"Job {job_id}: Cannot build cleanup command, going to ERROR directly")
                self._add_log_entry(job_id, "stderr",
                    "[CLEANUP] Warning: unable to build revert command, manual cleanup may be needed")
                await self.transition_to(job_id, Stage.ERROR)
            return

        # Execute command for the new stage
        command = self._get_stage_command(job, next_stage)
        
        # Special handling for P4PUSH - replace {changelist} placeholder
        if next_stage == Stage.P4PUSH and command:
            changelist = job.get("changelist")
            if changelist:
                command = command.replace("{changelist}", str(changelist))
                logger.info(f"P4PUSH command with changelist {changelist}: {command}")
            else:
                error_msg = f"[FLOW ERROR] No changelist available for P4PUSH stage"
                logger.error(f"No changelist available for P4PUSH in job {job_id}")
                self._add_log_entry(job_id, "stderr", error_msg)
                job["error"] = error_msg
                await self.transition_to(job_id, Stage.ERROR)
                return
        
        if command:
            await self._execute_command(job_id, command)
        else:
            # If no command defined, check if it's an optional stage
            if next_stage == Stage.PRE_SUBMIT:
                # PRE_SUBMIT is optional - skip to SHELVE if no hook defined
                logger.info(f"No pre-submit hook defined for job {job_id}, skipping to SHELVE")
                await self.transition_to(job_id, Stage.SHELVE)
            else:
                # Required stage has no command - this is an error
                # Note: If config validation failed in _get_stage_command, job["error"] is already set
                if not job.get("error"):
                    error_msg = f"[FLOW ERROR] No command defined for stage {next_stage.value}"
                    self._add_log_entry(job_id, "stderr", error_msg)
                    job["error"] = error_msg
                logger.error(f"No command defined for stage {next_stage}")
                await self.transition_to(job_id, Stage.ERROR)

    def _get_stage_command(self, job: dict, stage: Stage) -> Optional[str]:
        """Get shell command for stage"""
        spec = job["spec"]
        job_id = job.get("job_id")
        
        # Get workspace
        workspace = spec.get("workspace", "")
        if not workspace:
            error_msg = "[CONFIG ERROR] No workspace specified in job spec!"
            logger.error(error_msg)
            if job_id:
                self._add_log_entry(job_id, "stderr", error_msg)
                job["error"] = error_msg
            return None
        
        # Get P4 configuration from job spec
        p4_config = spec.get("p4", {})
        p4_bin = "/tool/pandora64/bin/p4"  # Hardcoded per requirements
        p4_port = p4_config.get("port", "")
        p4_client = p4_config.get("client", "")
        p4_user = p4_config.get("user", "")
        p4_password = p4_config.get("password", "")
        
        # Validate required P4 fields with user-visible error logging
        if not p4_port:
            error_msg = "[CONFIG ERROR] P4PORT not provided in job spec!"
            logger.error(error_msg)
            if job_id:
                self._add_log_entry(job_id, "stderr", error_msg)
                job["error"] = error_msg
            return None
        if not p4_client:
            error_msg = "[CONFIG ERROR] P4CLIENT not provided in job spec!"
            logger.error(error_msg)
            if job_id:
                self._add_log_entry(job_id, "stderr", error_msg)
                job["error"] = error_msg
            return None
        if not p4_user:
            error_msg = "[CONFIG ERROR] P4USER not provided in job spec!"
            logger.error(error_msg)
            if job_id:
                self._add_log_entry(job_id, "stderr", error_msg)
                job["error"] = error_msg
            return None
        if not p4_password:
            error_msg = "[CONFIG ERROR] P4PASSWD not provided in job spec!"
            logger.error(error_msg)
            if job_id:
                self._add_log_entry(job_id, "stderr", error_msg)
                job["error"] = error_msg
            return None
        
        logger.info(f"Using P4 - binary: {p4_bin}, client: {p4_client}, port: {p4_port}, user: {p4_user}")
        
        # Explicitly use single quotes to wrap password to handle special chars like !
        # And escape any single quotes inside the password itself
        safe_password = p4_password.replace("'", "'\"'\"'")
        p4_password_arg = f"'{safe_password}'"
        
        # Hardcoded init script path
        init_script = "/proj/verif_release_ro/cbwa_initscript/current/cbwa_init.bash"
        
        # Handle INTEGRATE command variations
        branch_spec = spec.get('branch_spec')
        source = spec.get('source', '')
        target = spec.get('target', '')
        # Use fetched source_changelist (from GET_LATEST_CL) if available, otherwise fall back to spec
        source_rev_change = job.get('source_changelist') or spec.get('changelist')
        
        # Build integrate command with explicit parameters
        integrate_cmd = ""
        # Base P4 command with all explicit parameters
        # NOTE: We construct the command string manually with quotes to avoid shlex issues with !
        p4_base = f"{p4_bin} -p {p4_port} -u {p4_user} -c {p4_client} -P {p4_password_arg}"
        
        if branch_spec:
            # Branch mode
            if source_rev_change:
                integrate_cmd = f"{p4_base} integrate -b {branch_spec} ...@{int(source_rev_change)}"
            else:
                # Use latest if not specified
                integrate_cmd = f"{p4_base} integrate -b {branch_spec}"
        elif source and target:
            # Direct mode
            src_arg = source + (f"@{source_rev_change}" if source_rev_change else "")
            integrate_cmd = f"{p4_base} integrate {src_arg} {target}"
        else:
            # Fallback (will likely fail)
            path = spec.get('path', '')
            integrate_cmd = f"{p4_base} integrate {path}"
        
        # Build SHELVE command (complex multi-step) with explicit parameters
        # Get a meaningful name for the spec
        spec_name = branch_spec or spec.get('spec_name', 'N/A')
        user_description = spec.get('description', '')
        
        # Build description with user input if provided
        if user_description:
            desc_text = f'\\tREVIEW_INTEGRATE\\n\\t[INFRAFIX] Mass integration from {source or branch_spec or "unknown"} @{source_rev_change or "latest"}\\n\\tSPEC: {spec_name}\\n\\t{user_description}'
        else:
            desc_text = f'\\tREVIEW_INTEGRATE\\n\\t[INFRAFIX] Mass integration from {source or branch_spec or "unknown"} @{source_rev_change or "latest"}\\n\\tSPEC: {spec_name}'
        
        # Get name_check tool path from config
        name_check_tool = self.config.get("env_init", {}).get("name_check_tool", "/tool/aticad/1.0/src/perforce/name_check_file_list")
        max_nc_passes = self.config.get("name_check", {}).get("max_passes", 5)
        
        shelve_cmd = f"""
# Create changelist with full description
DESC=$'{desc_text}'

# Create changelist and extract number
# p4 change -i outputs: "Change 8374786 created." or "Change 8374786 saved."
cl_output=$({p4_base} change -o | awk -v desc="$DESC" '/^Description:/{{print; print desc; in_desc=1; next}} in_desc && /^\\t/{{next}} in_desc && /^[^\\t]/{{in_desc=0}} {{print}}' | {p4_base} change -i)

# Extract CL number using sed (more portable than grep -oP)
cl=$(echo "$cl_output" | sed -n 's/Change \\([0-9][0-9]*\\) .*/\\1/p')

# Check if changelist was created
if [ -z "$cl" ]; then
  echo "ERROR: Failed to create changelist"
  echo "Output was: $cl_output"
  exit 1
fi

echo "Created changelist: $cl"

# Move all opened files to new CL
{p4_base} reopen -c $cl //...

# Initial shelve
echo "Shelving changelist $cl..."
{p4_base} shelve -f -c $cl

# ========== name_check remediation ==========
NC_TOOL="{name_check_tool}"
MAX_PASSES={max_nc_passes}

if [ ! -x "$NC_TOOL" ]; then
  echo "WARNING: name_check tool not found at $NC_TOOL, skipping remediation"
else
  echo "Starting name_check remediation (max $MAX_PASSES passes)..."
  tries=0
  while [ $tries -lt $MAX_PASSES ]; do
    tries=$((tries+1))
    echo "name_check pass $tries/$MAX_PASSES"
    
    rm -f /tmp/name_check_file_list_$cl 2>/dev/null
    {p4_base} shelve -c $cl 2>&1 | "$NC_TOOL" > /tmp/name_check_file_list_$cl || true
    
    if [ -s /tmp/name_check_file_list_$cl ]; then
      echo "Offending files found:"
      cat /tmp/name_check_file_list_$cl
      echo "Reverting offending files..."
      {p4_base} -x /tmp/name_check_file_list_$cl revert || echo "WARNING: revert failed"
      echo "Reshelving (-r)..."
      {p4_base} shelve -r -c $cl || echo "WARNING: reshelve failed"
    else
      echo "name_check: no offending files found"
      break
    fi
  done
  
  if [ $tries -eq $MAX_PASSES ]; then
    echo "WARNING: Reached max passes ($MAX_PASSES), some name_check issues may remain"
  fi
  
  # Cleanup temp file
  rm -f /tmp/name_check_file_list_$cl 2>/dev/null
fi

# Output changelist number for parsing
echo "CHANGELIST:$cl"
""".strip()
        
        # Build GET_LATEST_CL command (password safely quoted)
        get_latest_cl_cmd = ""
        if branch_spec:
            # Get latest CL from branch spec
            get_latest_cl_cmd = f"{p4_bin} -p {p4_port} -u {p4_user} -P {p4_password_arg} branch -o {branch_spec} | grep '//' | head -n 1 | awk '{{print $1}}' | xargs -I {{}} {p4_bin} -p {p4_port} -u {p4_user} -P {p4_password_arg} changes -m 1 -s submitted {{}} | awk '{{print $2}}'"
        elif source:
            # Get latest CL from source path
            get_latest_cl_cmd = f"{p4_bin} -p {p4_port} -u {p4_user} -P {p4_password_arg} changes -m 1 -s submitted {source}... | awk '{{print $2}}'"
        
        # Build P4PUSH command with trial support
        trial_flag = "-trial" if spec.get("trial") else ""
        # Use job["changelist"] which will be set after SHELVE completes
        p4push_cmd = f"cd {workspace} && source {init_script} && bootenv && p4push {trial_flag} -c {{changelist}}"
        # Note: {changelist} placeholder will be replaced in transition_to when we have the actual CL
        
        commands = {
            Stage.GET_LATEST_CL: get_latest_cl_cmd,
            Stage.SYNC: f"cd {workspace} && source {init_script} && bootenv && p4w sync_all -bsc",
            Stage.INTEGRATE: integrate_cmd,
            Stage.RESOLVE_PASS_1: f"{p4_base} resolve -am",
            Stage.RESOLVE_CHECK: f"{p4_base} resolve -n",
            Stage.PRE_SUBMIT: spec.get('pre_submit_hook'),
            Stage.SHELVE: shelve_cmd,
            Stage.CLEANUP: f"{p4_base} revert //...",
            Stage.P4PUSH: p4push_cmd
        }
        
        return commands.get(stage)
    
    async def _execute_command(self, job_id: str, command: str):
        """Dispatch command to Agent"""
        job = self.jobs[job_id]
        cmd_id = str(uuid.uuid4())
        
        job["current_cmd_id"] = cmd_id
        self.cmd_to_job[cmd_id] = job_id
        
        # Send to agent
        try:
            await self.agent_server.send_to_agent(job["agent_id"], {
                "type": "EXEC_CMD",
                "cmd_id": cmd_id,
                "command": command,
                "cwd": job["spec"]["workspace"],
                "env": job["spec"].get("env", {})
            })
            logger.info(f"Sent command {cmd_id} to agent for job {job_id}")
        except Exception as e:
            error_msg = f"[NETWORK ERROR] Failed to send command to agent: {e}"
            logger.error(f"Failed to send command to agent: {e}")
            self._add_log_entry(job_id, "stderr", error_msg)
            job["error"] = error_msg
            await self.transition_to(job_id, Stage.ERROR)
    
    async def _shutdown_agent(self, job_id: str, reason: str = ""):
        """Send shutdown signal to the agent associated with a job.
        
        Called when job enters terminal state (DONE or ERROR) to clean up
        the remote agent process and free resources.
        """
        job = self.jobs.get(job_id)
        if not job:
            return
        
        agent_id = job.get("agent_id")
        if not agent_id:
            logger.warning(f"Job {job_id} has no agent_id, cannot send shutdown")
            return
        
        # Check if agent is still connected
        if agent_id not in self.agent_server.agents:
            logger.info(f"Agent {agent_id} already disconnected, no shutdown needed")
            return
        
        try:
            await self.agent_server.send_to_agent(agent_id, {
                "type": "SHUTDOWN",
                "reason": reason
            })
            self._add_log_entry(job_id, "stdout", f"[INFO] Agent shutdown signal sent ({reason})")
            logger.info(f"Sent SHUTDOWN to agent {agent_id} for job {job_id}: {reason}")
        except Exception as e:
            logger.warning(f"Failed to send shutdown to agent {agent_id}: {e}")
            # Not a critical error - agent might already be disconnected

    async def handle_agent_event(self, agent_id: str, message: dict):
        """Handle incoming events from Agent"""
        msg_type = message.get("type")
        
        if msg_type == "LOG":
            await self._handle_log(message)
        elif msg_type == "CMD_STARTED":
            await self._handle_cmd_started(message)
        elif msg_type == "CMD_DONE":
            await self._handle_cmd_done(message)
        elif msg_type == "AGENT_TIMEOUT":
            await self._handle_agent_timeout(agent_id, message)
        elif msg_type == "AGENT_DISCONNECTED":
            await self._handle_agent_disconnected(agent_id, message)
    
    async def _handle_agent_timeout(self, agent_id: str, message: dict):
        """Handle agent timeout - fail all jobs associated with this agent"""
        logger.warning(f"Agent {agent_id} timed out: {message}")
        
        # Find all jobs using this agent
        affected_jobs = [
            job_id for job_id, job in self.jobs.items()
            if job.get("agent_id") == agent_id and job.get("stage") not in [Stage.DONE.value, Stage.ERROR.value]
        ]
        
        for job_id in affected_jobs:
            job = self.jobs[job_id]
            current_stage = job.get("stage", "unknown")
            error_msg = f"Agent connection lost (no heartbeat for {message.get('timeout_seconds', 30):.0f}s)"
            
            # Log the error
            self._add_log_entry(job_id, "stderr", f"ERROR: {error_msg}")
            
            # Warn about manual cleanup if files may be opened
            # (agent is gone so we can't run CLEANUP/revert automatically)
            if current_stage in _NEEDS_CLEANUP_STAGE_VALUES:
                p4_client = job.get("spec", {}).get("p4", {}).get("client", "unknown")
                self._add_log_entry(job_id, "stderr",
                    f"[WARNING] Agent timed out during stage with opened files. "
                    f"Manual cleanup may be needed: p4 -c {p4_client} revert //...")
            
            # Set error before transition_to so it's preserved
            job["error"] = error_msg
            
            # Use transition_to for proper cleanup (workspace release, monitor stop, agent shutdown)
            await self.transition_to(job_id, Stage.ERROR)
            logger.error(f"Job {job_id} failed due to agent timeout")
    
    async def _handle_agent_disconnected(self, agent_id: str, message: dict):
        """Handle agent disconnection - log to affected jobs but don't fail them immediately
        
        Unlike timeout, disconnection might be intentional (job completed) or due to network issues.
        We log the event but let the job continue if it's already in a terminal state.
        """
        logger.info(f"Agent {agent_id} disconnected: {message}")
        
        # Find all jobs using this agent that are still running
        affected_jobs = [
            job_id for job_id, job in self.jobs.items()
            if job.get("agent_id") == agent_id and job.get("stage") not in [Stage.DONE.value, Stage.ERROR.value]
        ]
        
        for job_id in affected_jobs:
            job = self.jobs[job_id]
            current_stage = job.get("stage", "unknown")
            
            # Log the disconnection to user-visible log
            disconnect_msg = f"[CONNECTION] Agent disconnected (hostname: {message.get('hostname', 'unknown')})"
            self._add_log_entry(job_id, "stderr", disconnect_msg)
            
            # If job was actively running a command, mark it as failed
            if job.get("current_cmd_id"):
                error_msg = f"Agent connection lost during stage {current_stage}"
                self._add_log_entry(job_id, "stderr", f"[CONNECTION ERROR] {error_msg}")
                
                # Warn about manual cleanup if files may be opened
                # (agent is gone so we can't run CLEANUP/revert automatically)
                if current_stage in _NEEDS_CLEANUP_STAGE_VALUES:
                    p4_client = job.get("spec", {}).get("p4", {}).get("client", "unknown")
                    self._add_log_entry(job_id, "stderr",
                        f"[WARNING] Agent lost during stage with opened files. "
                        f"Manual cleanup may be needed: p4 -c {p4_client} revert //...")
                
                # Set error before transition_to so it's preserved
                job["error"] = error_msg
                
                # Use transition_to for proper cleanup (workspace release, monitor stop, agent shutdown)
                await self.transition_to(job_id, Stage.ERROR)
                logger.error(f"Job {job_id} failed due to agent disconnection during active command")

    async def _handle_cmd_started(self, message: dict):
        """Handle command started - store PID"""
        cmd_id = message.get("cmd_id")
        pid = message.get("pid")
        
        if cmd_id in self.cmd_to_job:
            job_id = self.cmd_to_job[cmd_id]
            job = self.jobs[job_id]
            stage = job.get("stage", "unknown")
            
            # Initialize pids dict if not exists
            if "pids" not in job:
                job["pids"] = {}
            
            # Store PID with stage name as key
            job["pids"][stage] = pid
            logger.info(f"Job {job_id} stage {stage} started with PID {pid}")
            
    async def _handle_log(self, message: dict):
        """Store logs in memory and persist to file"""
        cmd_id = message.get("cmd_id")
        if cmd_id in self.cmd_to_job:
            job_id = self.cmd_to_job[cmd_id]
            entry = {
                "cmd_id": cmd_id,  # Add cmd_id for filtering
                "stream": message.get("stream"),
                "data": message.get("data"),
                "timestamp": datetime.now().isoformat()
            }
            self.logs[job_id].append(entry)
            
            # Persist to file
            self._append_log_to_file(job_id, entry)
            
    async def _handle_cmd_done(self, message: dict):
        """Handle command completion"""
        cmd_id = message.get("cmd_id")
        exit_code = message.get("exit_code")
        agent_error = message.get("error")  # Error message from agent (e.g., path not found)
        
        if cmd_id not in self.cmd_to_job:
            return
            
        job_id = self.cmd_to_job[cmd_id]
        job = self.jobs[job_id]
        current_stage = Stage(job["stage"])
        
        logger.info(f"Command {cmd_id} finished: code={exit_code}, job={job_id}, stage={current_stage.value}")
        
        # Log agent errors and non-zero exit codes to user-visible log
        if exit_code != 0:
            if agent_error:
                # Agent sent a specific error message (e.g., FileNotFoundError, PermissionError)
                error_msg = f"[AGENT ERROR] {agent_error}"
                self._add_log_entry(job_id, "stderr", error_msg)
                job["error"] = error_msg
            else:
                # Command failed but no specific error from agent
                error_msg = f"[COMMAND FAILED] Stage {current_stage.value} exited with code {exit_code}"
                self._add_log_entry(job_id, "stderr", error_msg)
                if not job.get("error"):  # Don't overwrite more specific errors
                    job["error"] = error_msg
        
        next_stage = await self._decide_next_stage(job_id, current_stage, exit_code)
        if next_stage:
            await self.transition_to(job_id, next_stage)
            
    async def _decide_next_stage(self, job_id: str, current_stage: Stage, exit_code: int) -> Optional[Stage]:
        """Determine next stage based on result"""
        
        # CLEANUP always goes to ERROR regardless of exit code
        if current_stage == Stage.CLEANUP:
            if exit_code == 0:
                self._add_log_entry(job_id, "stdout", "[CLEANUP] Workspace cleaned successfully")
            else:
                self._add_log_entry(job_id, "stderr", 
                    f"[CLEANUP] Warning: revert failed (exit code {exit_code}), manual cleanup may be needed")
            return Stage.ERROR
        
        # Handle non-zero exit codes
        if exit_code != 0:
            # RESOLVE_CHECK special case: p4 resolve -n may return non-zero
            # when there are no files to resolve (varies by P4 server version).
            # Always analyze the output instead of failing immediately.
            if current_stage == Stage.RESOLVE_CHECK:
                logger.info(f"Job {job_id}: RESOLVE_CHECK exited with code {exit_code}, analyzing output")
                return self._analyze_resolve_check(job_id)
            
            # For other stages, route to CLEANUP if files may be opened
            if current_stage.value in _NEEDS_CLEANUP_STAGE_VALUES:
                return Stage.CLEANUP
            return Stage.ERROR
            
        # Success transitions
        transitions = {
            Stage.GET_LATEST_CL: Stage.SYNC,  # After getting latest CL, go to SYNC
            Stage.SYNC: Stage.INTEGRATE,
            Stage.INTEGRATE: Stage.RESOLVE_PASS_1,
            Stage.RESOLVE_PASS_1: Stage.RESOLVE_CHECK,  # Skip redundant PASS_2, go straight to CHECK
            # RESOLVE_CHECK needs output analysis
            # PRE_SUBMIT needs output analysis (implied hook success if exit_code=0)
            Stage.PRE_SUBMIT: Stage.SHELVE,
            # SHELVE needs output analysis (name_check remediation is handled inline)
            Stage.P4PUSH: Stage.DONE
        }
        
        # Stages that need output analysis
        if current_stage == Stage.GET_LATEST_CL:
            return self._analyze_get_latest_cl(job_id)
        elif current_stage == Stage.RESOLVE_CHECK:
            return self._analyze_resolve_check(job_id)
        elif current_stage == Stage.SHELVE:
            return self._analyze_shelve_output(job_id)
        elif current_stage == Stage.INTEGRATE:
            return self._analyze_integrate_output(job_id)
        elif current_stage == Stage.RESOLVE_PASS_1:
            self._log_resolve_summary(job_id)
            return Stage.RESOLVE_CHECK
        
        return transitions.get(current_stage, Stage.ERROR)

    def _analyze_get_latest_cl(self, job_id: str) -> Stage:
        """Parse latest changelist number from GET_LATEST_CL output"""
        job = self.jobs[job_id]
        
        # Get stdout output
        stdout_output = "".join([l["data"] for l in self.logs[job_id] if l["stream"] == "stdout"])
        
        # Output should be a pure number (e.g., "8372976")
        cl_number = stdout_output.strip()
        
        if cl_number.isdigit():
            job["source_changelist"] = cl_number
            logger.info(f"Got latest changelist {cl_number} for job {job_id}")
            return Stage.SYNC
        else:
            # Truncate output for display
            output_preview = stdout_output[:200] if len(stdout_output) > 200 else stdout_output
            error_msg = f"[PARSE ERROR] Failed to parse changelist from GET_LATEST_CL output: '{output_preview}'"
            logger.error(f"Failed to parse changelist from output: {stdout_output}")
            self._add_log_entry(job_id, "stderr", error_msg)
            job["error"] = error_msg
            return Stage.ERROR
    
    def _analyze_resolve_check(self, job_id: str) -> Stage:
        """Analyze 'p4 resolve -n' output.
        
        Uses CONSERVATIVE fallback: if output is unclear or unparseable,
        assume conflicts exist rather than silently skipping them.
        Checks BOTH stdout and stderr since P4 may output to either stream.
        """
        job = self.jobs[job_id]
        current_cmd_id = job.get("current_cmd_id")
        
        # Collect ALL output (stdout + stderr) from the current command
        # P4 may output "No file(s) to resolve" to stderr in some versions
        all_output = "".join([
            l["data"] + "\n" for l in self.logs[job_id] 
            if l.get("cmd_id") == current_cmd_id
        ])
        
        # Check for "No file(s) to resolve" in ALL output (stdout + stderr)
        # This is the clearest signal that everything is resolved
        if "No file(s) to resolve" in all_output:
            job["conflicts"] = []
            logger.info(f"Job {job_id}: No conflicts remaining, proceeding to PRE_SUBMIT")
            return Stage.PRE_SUBMIT
        
        # Parse conflict files from all output
        # p4 resolve -n output format: "//depot/path/file.cpp - merging //from/path/file.cpp"
        conflicts = []
        for line in all_output.split('\n'):
            line_lower = line.lower()
            if 'merging' in line_lower or 'resolve skipped' in line_lower:
                # Extract file path (starts with //)
                match = re.match(r'^(//[^\s]+)', line)
                if match:
                    conflicts.append(match.group(1))
        
        # If we found conflicts, go to NEEDS_RESOLVE
        if conflicts:
            job["conflicts"] = conflicts
            logger.info(f"Job {job_id}: Found {len(conflicts)} unresolved files: {conflicts[:5]}...")
            return Stage.NEEDS_RESOLVE
        
        # CONSERVATIVE fallback: if output is empty or unclear, assume conflicts
        # may exist rather than silently skipping them. This prevents unresolved
        # conflicts from being pushed to production.
        output_preview = all_output.strip()[:500] if all_output.strip() else "(empty)"
        logger.warning(f"Job {job_id}: p4 resolve -n output unclear, treating as unresolved. Output: {output_preview}")
        self._add_log_entry(job_id, "stderr", 
            "[WARNING] p4 resolve -n produced unclear output. Treating as unresolved for safety. "
            "Please check manually and click Continue when resolved.")
        job["conflicts"] = ["(unable to parse - manual check required)"]
        return Stage.NEEDS_RESOLVE

    def _analyze_integrate_output(self, job_id: str) -> Stage:
        """Analyze 'p4 integrate' output to verify files were actually integrated.
        
        Detects cases like 'all revision(s) already integrated' where integrate
        succeeds (exit code 0) but produces no files to resolve/shelve/push.
        """
        job = self.jobs[job_id]
        current_cmd_id = job.get("current_cmd_id")
        
        # Collect all output (stdout + stderr)
        all_output = "".join([
            l["data"] + "\n" for l in self.logs[job_id]
            if l.get("cmd_id") == current_cmd_id
        ])
        
        # Check for "already integrated" - nothing to do
        if "already integrated" in all_output.lower():
            warning_msg = "[INTEGRATE] All revision(s) already integrated - nothing to do"
            logger.warning(f"Job {job_id}: {warning_msg}")
            self._add_log_entry(job_id, "stderr", warning_msg)
            job["error"] = warning_msg
            return Stage.ERROR
        
        # Check for "no such file" or empty integrate
        if "no such file" in all_output.lower() or "no file(s) to integrate" in all_output.lower():
            error_msg = "[INTEGRATE] No files found to integrate - check source/target paths"
            logger.error(f"Job {job_id}: {error_msg}")
            self._add_log_entry(job_id, "stderr", error_msg)
            job["error"] = error_msg
            return Stage.ERROR
        
        # Count integrated files for logging
        integrated_count = 0
        for line in all_output.split('\n'):
            # p4 integrate output: "//depot/path/file.cpp#3 - integrate from //source/path/file.cpp#2,#3"
            if '#' in line and ' - ' in line:
                integrated_count += 1
        
        if integrated_count > 0:
            self._add_log_entry(job_id, "stdout", f"[INTEGRATE] {integrated_count} file(s) scheduled for integration")
            logger.info(f"Job {job_id}: Integrated {integrated_count} files")
        else:
            # Output exists but no recognizable integration lines - warn but continue
            output_preview = all_output.strip()[:300] if all_output.strip() else "(empty)"
            logger.warning(f"Job {job_id}: Could not parse integrate file count. Output: {output_preview}")
        
        return Stage.RESOLVE_PASS_1
    
    def _log_resolve_summary(self, job_id: str):
        """Log a summary of what happened during RESOLVE_PASS_1 for visibility.
        
        Parses the resolve -am output to report how many files were auto-merged
        and how many were skipped (have conflicts).
        """
        job = self.jobs[job_id]
        current_cmd_id = job.get("current_cmd_id")
        
        # Collect all output (stdout + stderr)
        all_output = "".join([
            l["data"] + "\n" for l in self.logs[job_id]
            if l.get("cmd_id") == current_cmd_id
        ])
        
        auto_resolved = 0
        skipped = 0
        
        for line in all_output.split('\n'):
            line_lower = line.lower()
            if 'merging' in line_lower and ('resolve' not in line_lower or 'skipped' not in line_lower):
                auto_resolved += 1
            elif 'resolve skipped' in line_lower or 'non-text' in line_lower:
                skipped += 1
        
        summary = f"[RESOLVE] Auto-merge complete: {auto_resolved} file(s) resolved"
        if skipped > 0:
            summary += f", {skipped} file(s) skipped (need manual resolution)"
        
        self._add_log_entry(job_id, "stdout", summary)
        logger.info(f"Job {job_id}: {summary}")

    def _analyze_shelve_output(self, job_id: str) -> Stage:
        """Analyze 'p4 shelve' output for changelist
        
        Note: name_check remediation is now handled inline in the SHELVE command itself,
        so we no longer need to check for name_check issues here.
        """
        job = self.jobs[job_id]
        current_cmd_id = job.get("current_cmd_id")
        
        # Filter logs for the current command only
        current_cmd_logs = [
            l["data"] for l in self.logs.get(job_id, []) 
            if l.get("cmd_id") == current_cmd_id and l["stream"] == "stdout"
        ]
        stdout_output = "".join(current_cmd_logs)
        
        cl_match = re.search(r'CHANGELIST:(\d+)', stdout_output)
        if cl_match:
            changelist = int(cl_match.group(1))
            job["changelist"] = changelist
            logger.info(f"Parsed changelist {changelist} from SHELVE output for job {job_id}")
        else:
            # Truncate output for display
            output_preview = stdout_output[:200] if len(stdout_output) > 200 else stdout_output
            error_msg = f"[PARSE ERROR] Failed to parse changelist from SHELVE output. Expected 'CHANGELIST:<number>' but got: '{output_preview}'"
            logger.error(f"Failed to parse changelist from SHELVE output for job {job_id}")
            logger.error(f"SHELVE stdout was: {stdout_output[:500]}...")
            self._add_log_entry(job_id, "stderr", error_msg)
            job["error"] = error_msg
            return Stage.ERROR
        
        return Stage.P4PUSH

    def _start_conflict_monitor(self, job_id: str):
        """Start background task to periodically check if conflicts have been resolved.
        
        Runs 'p4 resolve -n' every 60 seconds. When it detects that all conflicts
        have been resolved (user resolved them manually), it automatically transitions
        the job to RESOLVE_CHECK for proper validation.
        """
        if job_id in self.monitor_tasks:
            return
            
        async def monitor():
            logger.info(f"Started conflict monitor for job {job_id}")
            check_interval = 60  # seconds between checks
            
            try:
                while True:
                    await asyncio.sleep(check_interval)
                    
                    # Check if job is still in NEEDS_RESOLVE
                    job = self.jobs.get(job_id)
                    if not job or job["stage"] != Stage.NEEDS_RESOLVE.value:
                        logger.info(f"Conflict monitor for {job_id}: job no longer in NEEDS_RESOLVE, stopping")
                        break
                    
                    # Check if agent is still connected
                    agent_id = job.get("agent_id")
                    if not agent_id or agent_id not in self.agent_server.agents:
                        logger.warning(f"Conflict monitor for {job_id}: agent not connected, stopping")
                        break
                    
                    try:
                        # Schedule the resolve check as a SEPARATE task to avoid
                        # cancellation issues: user_continue -> transition_to may call
                        # _stop_conflict_monitor which cancels THIS task. By spawning
                        # a separate task, the state transition won't be interrupted.
                        logger.info(f"Conflict monitor for {job_id}: triggering resolve check")
                        asyncio.create_task(self.user_continue(job_id))
                        
                        # Wait for the check to complete before next cycle
                        await asyncio.sleep(10)
                        
                    except Exception as e:
                        logger.error(f"Conflict monitor error for {job_id}: {e}")
                        continue
            except asyncio.CancelledError:
                pass  # Normal shutdown via _stop_conflict_monitor
            
            logger.info(f"Conflict monitor stopped for job {job_id}")
                
        self.monitor_tasks[job_id] = asyncio.create_task(monitor())

    def _stop_conflict_monitor(self, job_id: str):
        if job_id in self.monitor_tasks:
            self.monitor_tasks[job_id].cancel()
            del self.monitor_tasks[job_id]
    
    def _release_workspace(self, job_id: str):
        """Release the workspace when a job completes or fails."""
        if not self._workspace_queue:
            return
        
        job = self.jobs.get(job_id)
        if not job:
            return
        
        workspace = job.get("spec", {}).get("workspace")
        if not workspace:
            return
        
        try:
            # Release the workspace and get next queued job if any
            next_job = self._workspace_queue.release(workspace, job_id)
            
            if next_job:
                # Start the next queued job
                logger.info(f"Starting next queued job {next_job.job_id} for workspace {workspace}")
                # The callback will handle starting the job
        except Exception as e:
            logger.error(f"Failed to release workspace {workspace} for job {job_id}: {e}")

    def _add_log_entry(self, job_id: str, stream: str, data: str):
        """Add a log entry to a job's log (both in-memory and persisted to file).
        
        This is the unified interface for adding system-level logs that should be
        visible to users in the UI. Use this for:
        - Configuration errors
        - Flow/transition errors
        - Agent errors
        - Parsing errors
        
        Args:
            job_id: The job ID to add the log to
            stream: 'stdout' or 'stderr' (stderr will be highlighted as error in UI)
            data: The log message content
        """
        if job_id not in self.logs:
            self.logs[job_id] = []
        
        entry = {
            "cmd_id": None,  # System-generated logs don't have a cmd_id
            "stream": stream,
            "data": data,
            "timestamp": datetime.now().isoformat()
        }
        
        self.logs[job_id].append(entry)
        self._append_log_to_file(job_id, entry)
        
        logger.info(f"[Job {job_id}] {stream.upper()}: {data}")

    def _get_log_dir(self, job_id: str) -> str:
        """Get log directory for a job.
        
        Logs are stored in {workspace}/.p4_integ/logs/{job_id}/ if workspace is available,
        otherwise falls back to the global data directory.
        """
        job = self.jobs.get(job_id)
        workspace = job.get("spec", {}).get("workspace") if job else None
        
        if workspace:
            # Use workspace-based storage
            from app.config import get_workspace_data_dir
            try:
                ws_data_dir = get_workspace_data_dir(workspace)
                log_dir = os.path.join(ws_data_dir, "logs", job_id)
                os.makedirs(log_dir, exist_ok=True)
                return log_dir
            except Exception:
                pass  # Fall back to global data dir
        
        # Fallback: use global data directory
        data_dir = self.config.get("data_dir", "data")
        
        # If data_dir is not absolute, make it relative to the project root (parent of app/)
        if not os.path.isabs(data_dir):
            # Get project root (parent directory of app/)
            current_file = os.path.abspath(__file__)  # job_state_machine.py
            app_dir = os.path.dirname(os.path.dirname(current_file))  # app/
            project_root = os.path.dirname(app_dir)  # p4-integration/
            data_dir = os.path.join(project_root, data_dir)
        
        log_dir = os.path.join(data_dir, "logs", job_id)
        os.makedirs(log_dir, exist_ok=True)
        return log_dir

    def _append_log_to_file(self, job_id: str, entry: dict):
        """Append log entry to both txt and json files"""
        try:
            log_dir = self._get_log_dir(job_id)
            
            # Append to txt (human readable)
            txt_path = os.path.join(log_dir, "output.txt")
            with open(txt_path, "a", encoding="utf-8") as f:
                stream = entry.get("stream", "")
                data = entry.get("data", "")
                prefix = "[ERR] " if stream == "stderr" else ""
                f.write(f"{prefix}{data}\n")
            
            # Append to jsonl (structured)
            jsonl_path = os.path.join(log_dir, "output.jsonl")
            with open(jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.error(f"Failed to persist log for job {job_id}: {e}")

    def get_log_file_path(self, job_id: str) -> dict:
        """Get paths to log files"""
        log_dir = self._get_log_dir(job_id)
        return {
            "txt": os.path.join(log_dir, "output.txt"),
            "jsonl": os.path.join(log_dir, "output.jsonl"),
            "dir": log_dir
        }

    async def user_continue(self, job_id: str):
        """User clicked Continue button"""
        job = self.jobs.get(job_id)
        if job and job["stage"] == Stage.NEEDS_RESOLVE.value:
            # Trigger a check
            await self.transition_to(job_id, Stage.RESOLVE_CHECK)

    def get_job(self, job_id: str) -> Optional[dict]:
        job = self.jobs.get(job_id)
        if job:
            job_with_logs = job.copy()
            logs = self.logs.get(job_id, [])
            log_lines = []
            for entry in logs:
                line = entry.get("data", "")
                # Check if it's a real error
                is_real_error = any(err in line.lower() for err in [
                    'error:', 'fatal:', 'failed:', 'not found', 
                    'permission denied', 'traceback', 'exception'
                ])
                
                if entry.get("stream") == "stdout":
                    log_lines.append(line)
                elif entry.get("stream") == "stderr":
                    # Only add [ERROR] prefix for real errors
                    if is_real_error:
                        log_lines.append(f"[ERROR] {line}")
                    else:
                        log_lines.append(line)
            job_with_logs["log"] = log_lines
            
            # Add status field (same logic as get_job_info)
            stage = job.get('stage', 'INIT')
            if stage in ['DONE']: status = 'done'
            elif stage in ['ERROR']: status = 'error'
            elif stage in ['NEEDS_RESOLVE']: status = 'needs_resolve'
            elif stage in ['CLEANUP']: status = 'error'  # CLEANUP is a pre-error state
            else: status = 'running'
            job_with_logs["status"] = status
            
            return job_with_logs
        return None

    def get_job_logs(self, job_id: str) -> List[dict]:
        return self.logs.get(job_id, [])
    
    def get_jobs_by_owner(self, owner: str) -> List[dict]:
        """Get all jobs belonging to a specific owner"""
        return [job for job in self.jobs.values() if job.get('owner') == owner]
    
    def get_all_jobs(self) -> List[dict]:
        """Get all jobs (for admin view)"""
        return list(self.jobs.values())
    
    def get_job_info(self, job_id: str) -> dict:
        """Get job information for API"""
        job = self.jobs.get(job_id)
        if not job:
            return None
        
        stage = job.get('stage', 'INIT')
        if stage in ['DONE']: status = 'done'
        elif stage in ['ERROR']: status = 'error'
        elif stage in ['NEEDS_RESOLVE']: status = 'needs_resolve'
        elif stage in ['CLEANUP']: status = 'error'  # CLEANUP is a pre-error state
        else: status = 'running'
        
        return {
            'id': job_id,
            'status': status,
            'stage': stage,
            'owner': job.get('owner', 'unknown'),  # Job owner for filtering
            'spec': job.get('spec'),
            'created_at': job.get('created_at'),
            'updated_at': job.get('updated_at'),
            'error': job.get('error'),
            'agent_id': job.get('agent_id'),
            'changelist': job.get('changelist'),  # Shelved changelist
            'source_changelist': job.get('source_changelist'),  # Source changelist for integrate
            'conflicts': job.get('conflicts', []),
            'pids': job.get('pids', {})
        }
    
    async def cancel_job(self, job_id: str):
        """Cancel a running job"""
        job = self.jobs.get(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        
        # Send kill command to agent if connected
        agent_id = job.get("agent_id")
        current_cmd_id = job.get("current_cmd_id")
        
        if agent_id and agent_id in self.agent_server.agents and current_cmd_id:
            try:
                logger.info(f"Sending KILL_CMD to agent {agent_id} for cmd {current_cmd_id}")
                await self.agent_server.send_to_agent(agent_id, {
                    "type": "KILL_CMD",  # Changed from CANCEL_CMD to KILL_CMD
                    "cmd_id": current_cmd_id,
                    "signal": 15  # SIGTERM
                })
                logger.info(f"Kill signal sent successfully for job {job_id}")
            except Exception as e:
                logger.error(f"Failed to send kill to agent: {e}")
        else:
            logger.warning(f"Cannot kill job {job_id}: agent_id={agent_id}, cmd_id={current_cmd_id}, agent_connected={agent_id in self.agent_server.agents if agent_id else False}")
        
        job["error"] = "Cancelled by user"
        
        # Determine if cleanup is needed (files may be opened after INTEGRATE)
        current_stage = job.get("stage", "INIT")
        if current_stage in _NEEDS_CLEANUP_STAGE_VALUES:
            await self.transition_to(job_id, Stage.CLEANUP)
        else:
            await self.transition_to(job_id, Stage.ERROR)

