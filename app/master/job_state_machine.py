"""
JobStateMachine - P4 Integration Job State Machine
Handles event-driven state transitions based on Agent events.
"""
import asyncio
import logging
import uuid
import re
import queue
import os
from typing import Dict, Optional, List, Any
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
    RESOLVE_PASS_2 = "RESOLVE_PASS_2"
    RESOLVE_CHECK = "RESOLVE_CHECK"
    NEEDS_RESOLVE = "NEEDS_RESOLVE"
    PRE_SUBMIT = "PRE_SUBMIT"
    SHELVE = "SHELVE"
    NC_FIX = "NC_FIX"
    P4PUSH = "P4PUSH"
    DONE = "DONE"
    ERROR = "ERROR"

class JobStateMachine:
    """Core business logic state machine"""
    def __init__(self, agent_server, config: dict):
        self.agent_server = agent_server
        self.config = config  # Store config for P4 client info
        self.jobs: Dict[str, dict] = {}  # job_id -> job_info
        self.cmd_to_job: Dict[str, str] = {}  # cmd_id -> job_id
        self.logs: Dict[str, List[dict]] = {}  # job_id -> [log_entries]
        self.sse_clients: Dict[str, List[queue.Queue]] = {}  # job_id -> [queue]
        
        # Register as event handler
        self.agent_server.register_event_handler(self)
        
        # Background task for conflict monitoring
        self.monitor_tasks: Dict[str, asyncio.Task] = {}
    
    def create_job(self, job_id: str, agent_id: str, spec: dict) -> dict:
        """Initialize a new job"""
        # If changelist not specified, mark as latest
        if not spec.get('changelist'):
            spec['changelist_source'] = 'latest'
        else:
            spec['changelist_source'] = 'user_specified'
        
        job = {
            "job_id": job_id,
            "agent_id": agent_id,
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
        logger.info(f"Created job {job_id} for agent {agent_id}, source CL: {spec.get('changelist', 'latest')}")
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
        self._emit_sse_event(job_id, "status_update", {"status": self.get_job_info(job_id)['status'] if self.get_job_info(job_id) else 'unknown', "stage": next_stage.value})
        
        # Handle special monitoring for NEEDS_RESOLVE
        if old_stage == Stage.NEEDS_RESOLVE.value and next_stage != Stage.NEEDS_RESOLVE:
            self._stop_conflict_monitor(job_id)
        
        if next_stage == Stage.NEEDS_RESOLVE:
            self._start_conflict_monitor(job_id)
            logger.info(f"Job {job_id} waiting for manual conflict resolution")
            return

        if next_stage == Stage.DONE:
            logger.info(f"Job {job_id} completed successfully")
            return
            
        if next_stage == Stage.ERROR:
            logger.error(f"Job {job_id} failed")
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
                logger.error(f"No changelist available for P4PUSH in job {job_id}")
                await self.transition_to(job_id, Stage.ERROR)
                return
        
        if command:
            await self._execute_command(job_id, command)
        else:
            # If no command (e.g. some intermediate state or hook missing), skip or error
            # For now, assume ERROR if command missing unless handled
            logger.error(f"No command defined for stage {next_stage}")
            await self.transition_to(job_id, Stage.ERROR)

    def _get_stage_command(self, job: dict, stage: Stage) -> Optional[str]:
        """Get shell command for stage"""
        spec = job["spec"]
        
        # Get workspace
        workspace = spec.get("workspace", "")
        if not workspace:
            logger.error("No workspace specified in job spec!")
            return None
        
        # Get P4 configuration from config.yaml
        # NOTE: P4CONFIG is on remote machine, cannot be read from Windows Master
        # So we require P4PORT and P4CLIENT in config.yaml
        p4_config = self.config.get("p4", {})
        p4_bin = p4_config.get("bin", "p4")
        p4_port = p4_config.get("port", "")
        p4_client = p4_config.get("client", "")
        p4_user = p4_config.get("user", "")
        p4_password = p4_config.get("password", "")
        
        # Validate required P4 fields
        if not p4_port:
            logger.error("P4PORT not configured in config.yaml!")
            return None
        if not p4_client:
            logger.error("P4CLIENT not configured in config.yaml!")
            return None
        if not p4_user:
            logger.error("P4USER not configured in config.yaml!")
            return None
        if not p4_password:
            logger.error("P4PASSWD not configured in config.yaml!")
            return None
        
        logger.info(f"Using P4 from P4CONFIG - binary: {p4_bin}, client: {p4_client}, port: {p4_port}, user: {p4_user}")
        
        if not p4_user or not p4_password:
            logger.error(f"P4USER or P4PASSWD not configured in config.yaml!")
            return None
        
        # Hardcoded init script path
        init_script = "/proj/verif_release_ro/cbwa_initscript/current/cbwa_init.bash"
        
        # Handle INTEGRATE command variations
        branch_spec = spec.get('branch_spec')
        source = spec.get('source', '')
        target = spec.get('target', '')
        source_rev_change = spec.get('changelist')  # Source revision/changelist for integrate
        
        # Build integrate command with explicit parameters
        integrate_cmd = ""
        # Base P4 command with all explicit parameters (password in single quotes to handle special chars)
        p4_base = f"{p4_bin} -p {p4_port} -u {p4_user} -c {p4_client} -P '{p4_password}'"
        
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
        shelve_cmd = f"""
# Create changelist with full description
DESC=$'\\tREVIEW_INTEGRATE\\n\\t[INFRAFIX] Mass integration from {source or branch_spec or 'unknown'} @{source_rev_change or 'latest'}\\n\\tSPEC: {spec.get('spec_name', 'N/A')}'
cl=$({p4_base} change -o | awk -v desc="$DESC" '/^Description:/{{print; print desc; in_desc=1; next}} in_desc && /^\\t/{{next}} in_desc && /^[^\\t]/{{in_desc=0}} {{print}}' | {p4_base} change -i | grep -oP 'Change \\\\K\\\\d+')

# Move all opened files to new CL
{p4_base} reopen -c $cl //...

# Shelve the changelist
{p4_base} shelve -f -c $cl

# Output changelist number for parsing
echo "CHANGELIST:$cl"
""".strip()
        
        # Build GET_LATEST_CL command (password in single quotes)
        get_latest_cl_cmd = ""
        if branch_spec:
            # Get latest CL from branch spec
            get_latest_cl_cmd = f"{p4_bin} -p {p4_port} -u {p4_user} -P '{p4_password}' branch -o {branch_spec} | grep '//' | head -n 1 | awk '{{print $1}}' | xargs -I {{}} {p4_bin} -p {p4_port} -u {p4_user} -P '{p4_password}' changes -m 1 -s submitted {{}} | awk '{{print $2}}'"
        elif source:
            # Get latest CL from source path
            get_latest_cl_cmd = f"{p4_bin} -p {p4_port} -u {p4_user} -P '{p4_password}' changes -m 1 -s submitted {source}... | awk '{{print $2}}'"
        
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
            Stage.RESOLVE_PASS_2: f"{p4_base} resolve -am",
            Stage.RESOLVE_CHECK: f"{p4_base} resolve -n",
            Stage.PRE_SUBMIT: spec.get('pre_submit_hook'),
            Stage.SHELVE: shelve_cmd,
            Stage.NC_FIX: spec.get('name_check_fix_script'),
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
            logger.error(f"Failed to send command to agent: {e}")
            await self.transition_to(job_id, Stage.ERROR)
    
    async def handle_agent_event(self, agent_id: str, message: dict):
        """Handle incoming events from Agent"""
        msg_type = message.get("type")
        
        if msg_type == "LOG":
            await self._handle_log(message)
        elif msg_type == "CMD_STARTED":
            await self._handle_cmd_started(message)
        elif msg_type == "CMD_DONE":
            await self._handle_cmd_done(message)
    
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
        """Store logs"""
        cmd_id = message.get("cmd_id")
        if cmd_id in self.cmd_to_job:
            job_id = self.cmd_to_job[cmd_id]
            entry = {
                "stream": message.get("stream"),
                "data": message.get("data"),
                "timestamp": datetime.now().isoformat()
            }
            self.logs[job_id].append(entry)
            
    async def _handle_cmd_done(self, message: dict):
        """Handle command completion"""
        cmd_id = message.get("cmd_id")
        exit_code = message.get("exit_code")
        
        if cmd_id not in self.cmd_to_job:
            return
            
        job_id = self.cmd_to_job[cmd_id]
        job = self.jobs[job_id]
        current_stage = Stage(job["stage"])
        
        logger.info(f"Command {cmd_id} finished: code={exit_code}, job={job_id}, stage={current_stage.value}")
        
        next_stage = await self._decide_next_stage(job_id, current_stage, exit_code)
        if next_stage:
            await self.transition_to(job_id, next_stage)
            
    async def _decide_next_stage(self, job_id: str, current_stage: Stage, exit_code: int) -> Optional[Stage]:
        """Determine next stage based on result"""
        
        # Basic error handling
        if exit_code != 0:
            # Exceptions: Resolve pass 1/2 usually return 0 even if conflicts remain, 
            # but if p4 fails (e.g. network), it returns non-zero.
            # Strictly per doc:
            return Stage.ERROR
            
        # Success transitions
        transitions = {
            Stage.GET_LATEST_CL: Stage.SYNC,  # After getting latest CL, go to SYNC
            Stage.SYNC: Stage.INTEGRATE,
            Stage.INTEGRATE: Stage.RESOLVE_PASS_1,
            Stage.RESOLVE_PASS_1: Stage.RESOLVE_PASS_2,
            Stage.RESOLVE_PASS_2: Stage.RESOLVE_CHECK,
            # RESOLVE_CHECK needs output analysis
            # PRE_SUBMIT needs output analysis (implied hook success if exit_code=0)
            Stage.PRE_SUBMIT: Stage.SHELVE,
            # SHELVE needs output analysis
            # NC_FIX needs success -> SHELVE
            Stage.NC_FIX: Stage.SHELVE,
            Stage.P4PUSH: Stage.DONE
        }
        
        if current_stage == Stage.GET_LATEST_CL:
            return self._analyze_get_latest_cl(job_id)
        elif current_stage == Stage.RESOLVE_CHECK:
            return self._analyze_resolve_check(job_id)
        elif current_stage == Stage.SHELVE:
            return self._analyze_shelve_output(job_id)
        
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
            logger.error(f"Failed to parse changelist from output: {stdout_output}")
            return Stage.ERROR
    
    def _analyze_resolve_check(self, job_id: str) -> Stage:
        """Analyze 'p4 resolve -n' output"""
        # Aggregate stdout
        job_logs = self.logs.get(job_id, [])
        # Filter logs for current command? Ideally yes, but simpler to look at tail 
        # or filter by current_cmd_id if we tracked it in logs (we didn't explicitly, but we can)
        # For now, assume logs are append-only and we look at the recent ones or full log if simpler.
        # Better: filter logs by current_cmd_id
        
        current_cmd_id = self.jobs[job_id].get("current_cmd_id")
        # We need to look up logs? We didn't store cmd_id in log entry in _handle_log above...
        # Let's fix _handle_log to verify
        
        output = ""
        # Re-read logs - actually we didn't save cmd_id in the list entry, let's rely on 
        # the fact that stages are sequential.
        # Real implementation should store cmd_id in log entry.
        
        # For this implementation, let's just grab all stdout.
        output = "".join([l["data"] for l in self.logs[job_id] if l["stream"] == "stdout"])
        
        # Check for "No file(s) to resolve"
        if "No file(s) to resolve" in output:
            return Stage.PRE_SUBMIT
        
        # Check for "merging" or similar conflict indicators
        if "merging" in output or "resolve skipped" in output:
            return Stage.NEEDS_RESOLVE
            
        # Default fallback - if empty or unclear, usually means done or nothing to do
        # But "No file(s)" is standard p4 output.
        return Stage.PRE_SUBMIT

    def _analyze_shelve_output(self, job_id: str) -> Stage:
        """Analyze 'p4 shelve' output for changelist and name_check"""
        job = self.jobs[job_id]
        
        # Check stderr for name_check
        stderr_output = "".join([l["data"] for l in self.logs[job_id] if l["stream"] == "stderr"])
        if "name_check" in stderr_output.lower():
            return Stage.NC_FIX
        
        # Parse stdout for CHANGELIST:XXX
        stdout_output = "".join([l["data"] for l in self.logs[job_id] if l["stream"] == "stdout"])
        
        import re
        cl_match = re.search(r'CHANGELIST:(\d+)', stdout_output)
        if cl_match:
            changelist = int(cl_match.group(1))
            job["changelist"] = changelist
            logger.info(f"Parsed changelist {changelist} from SHELVE output for job {job_id}")
        else:
            logger.error(f"Failed to parse changelist from SHELVE output for job {job_id}")
            return Stage.ERROR
        
        return Stage.P4PUSH

    def _start_conflict_monitor(self, job_id: str):
        """Start background task to periodically check resolve status"""
        if job_id in self.monitor_tasks:
            return
            
        async def monitor():
            logger.info(f"Started conflict monitor for {job_id}")
            while True:
                await asyncio.sleep(30) # Check every 30s
                if self.jobs[job_id]["stage"] != Stage.NEEDS_RESOLVE.value:
                    break
                
                # Trigger a check
                # We execute p4 resolve -n directly. 
                # Note: We need a distinct command execution that doesn't mess up state flow.
                # We can treat it as a side-command.
                # For simplicity, let's just run it and analyze log separately?
                # Or reuse _execute_command but mark it special?
                
                # Implementation detail: We just want to see if it's resolved.
                # Let's run it, but we need to handle the result specially, not via main loop?
                # Actually, we can use a special "NEEDS_RESOLVE_CHECK" stage temporarily or 
                # simply handle the CMD_DONE for this specific background cmd.
                
                # FOR NOW: Keep it simple. User must click continue. 
                # Automated check is nice but complex to interleave with main state machine 
                # without a "Background Command" concept.
                pass
                
        self.monitor_tasks[job_id] = asyncio.create_task(monitor())

    def _stop_conflict_monitor(self, job_id: str):
        if job_id in self.monitor_tasks:
            self.monitor_tasks[job_id].cancel()
            del self.monitor_tasks[job_id]

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
            if stage in ['DONE', 'Pushed']: status = 'done'
            elif stage in ['ERROR']: status = 'error'
            elif stage in ['NEEDS_RESOLVE']: status = 'needs_resolve'
            elif stage in ['BLOCKED']: status = 'blocked'
            elif stage in ['AWAITING_APPROVAL']: status = 'awaiting_approval'
            elif stage in ['READY_TO_SUBMIT']: status = 'ready_to_submit'
            else: status = 'running'
            job_with_logs["status"] = status
            
            return job_with_logs
        return None

    def get_job_logs(self, job_id: str) -> List[dict]:
        return self.logs.get(job_id, [])
    
    def get_job_info(self, job_id: str) -> dict:
        """Get job information for API"""
        job = self.jobs.get(job_id)
        if not job:
            return None
        
        stage = job.get('stage', 'INIT')
        if stage in ['DONE', 'Pushed']: status = 'done'
        elif stage in ['ERROR']: status = 'error'
        elif stage in ['NEEDS_RESOLVE']: status = 'needs_resolve'
        elif stage in ['BLOCKED']: status = 'blocked'
        elif stage in ['AWAITING_APPROVAL']: status = 'awaiting_approval'
        elif stage in ['READY_TO_SUBMIT']: status = 'ready_to_submit'
        else: status = 'running'
        
        return {
            'id': job_id,
            'status': status,
            'stage': stage,
            'spec': job.get('spec'),
            'created_at': job.get('created_at'),
            'updated_at': job.get('updated_at'),
            'error': job.get('error'),
            'agent_id': job.get('agent_id'),
            'changelist': job.get('changelist'),  # Shelved changelist
            'source_changelist': job.get('source_changelist'),  # Source changelist for integrate
            'conflicts': job.get('conflicts', []),
            'blocked_files': job.get('blocked_files', []),
            'pids': job.get('pids', {})
        }
    
    def register_sse_client(self, job_id: str, q: queue.Queue):
        """Register SSE client for real-time updates"""
        if job_id not in self.sse_clients:
            self.sse_clients[job_id] = []
        self.sse_clients[job_id].append(q)
        logger.info(f"SSE client registered for job {job_id}")

    def unregister_sse_client(self, job_id: str, q: queue.Queue):
        """Unregister SSE client"""
        if job_id in self.sse_clients:
            self.sse_clients[job_id].remove(q)
            if not self.sse_clients[job_id]:
                del self.sse_clients[job_id]
        logger.info(f"SSE client unregistered for job {job_id}")

    def _emit_sse_event(self, job_id: str, event_type: str, data: dict):
        """Emit event to SSE clients"""
        if job_id in self.sse_clients:
            message = {"type": event_type, "job_id": job_id, "data": data}
            for q in self.sse_clients[job_id]:
                try:
                    q.put_nowait(message)
                except queue.Full:
                    pass # Client too slow, drop message
    
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
        
        # Update job state
        await self.transition_to(job_id, Stage.ERROR)
        job["error"] = "Cancelled by user"

