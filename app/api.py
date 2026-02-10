from flask import Blueprint, jsonify, request, current_app, redirect, url_for, session
from app import state_machine, agent_server, workspace_queue, scheduler_manager
from app.master.bootstrapper import Bootstrapper
from app.models.template import template_manager
import asyncio
import uuid

bp = Blueprint('api', __name__, url_prefix='/api')


# ============= Template API =============

@bp.route('/templates', methods=['GET'])
def list_templates():
    """List all templates available to the current user.
    
    Query params:
    - type: Filter by type ('global', 'private', or 'all')
    """
    username = session.get('p4_user')
    template_type = request.args.get('type', 'all')
    
    # Private templates now use centralized storage, no workspace needed
    if template_type == 'global':
        templates = template_manager.list_global_templates()
    elif template_type == 'private' and username:
        templates = template_manager.list_user_templates(username=username)
    else:
        templates = template_manager.list_all_templates(username=username)
    
    return jsonify({"templates": templates, "count": len(templates)})


@bp.route('/templates', methods=['POST'])
def create_template():
    """Create a new template.
    
    Request JSON:
    - name: Template name (required)
    - config: Template configuration (required)
    - type: "global" or "private" (default: "private")
    - workspace: Optional, stored in config but not used for storage location
    """
    data = request.json
    if not data:
        return jsonify({"error": "Request body is required"}), 400
    
    name = data.get('name')
    config = data.get('config')
    template_type = data.get('type', 'private')
    username = session.get('p4_user')
    
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not config:
        return jsonify({"error": "config is required"}), 400
    if not username:
        return jsonify({"error": "Not logged in"}), 401
    
    # Private templates now use centralized storage, no workspace needed for storage
    template = template_manager.create_template(
        name=name,
        config=config,
        owner=username,
        template_type=template_type
    )
    
    if template:
        return jsonify({"template": template}), 201
    return jsonify({"error": "Failed to create template"}), 500


@bp.route('/templates/<template_id>', methods=['GET'])
def get_template(template_id):
    """Get a template by ID."""
    username = session.get('p4_user')
    
    # Private templates now use centralized storage, no workspace needed
    template = template_manager.get_template(template_id, username=username)
    if template:
        return jsonify(template)
    return jsonify({"error": "Template not found"}), 404


@bp.route('/templates/<template_id>', methods=['PUT'])
def update_template(template_id):
    """Update a template.
    
    Request JSON:
    - name: New name (optional)
    - config: New config (optional)
    """
    data = request.json
    if not data:
        return jsonify({"error": "Request body is required"}), 400
    
    username = session.get('p4_user')
    
    if not username:
        return jsonify({"error": "Not logged in"}), 401
    
    updates = {}
    if 'name' in data:
        updates['name'] = data['name']
    if 'config' in data:
        updates['config'] = data['config']
    
    # Private templates now use centralized storage, no workspace needed
    template = template_manager.update_template(template_id, updates, username=username)
    if template:
        return jsonify({"template": template})
    return jsonify({"error": "Template not found or permission denied"}), 404


@bp.route('/templates/<template_id>', methods=['DELETE'])
def delete_template(template_id):
    """Delete a template."""
    username = session.get('p4_user')
    
    if not username:
        return jsonify({"error": "Not logged in"}), 401
    
    # Private templates now use centralized storage, no workspace needed
    success = template_manager.delete_template(template_id, username=username)
    if success:
        return jsonify({"status": "deleted"})
    return jsonify({"error": "Template not found or permission denied"}), 404


@bp.route('/templates/<template_id>/run', methods=['POST'])
def run_template(template_id):
    """Create and run a job from a template.
    
    Request JSON (optional overrides):
    - workspace: Override workspace path
    - changelist: Override changelist
    - trial: Override trial mode
    """
    data = request.json or {}
    username = session.get('p4_user')
    p4_password = session.get('p4_password')
    
    if not username or not p4_password:
        return jsonify({"error": "Not logged in"}), 401
    
    # Get template (centralized storage, no workspace needed for lookup)
    template = template_manager.get_template(template_id, username=username)
    if not template:
        return jsonify({"error": "Template not found"}), 404
    
    # Build spec from template config
    config = template.get('config', {})
    spec = {
        "workspace": data.get('workspace') or config.get('workspace'),
        "branch_spec": config.get('branch_spec'),
        "changelist": data.get('changelist') or config.get('changelist'),
        "path": config.get('path', ''),
        "description": config.get('description', ''),
        "trial": data.get('trial') if 'trial' in data else config.get('trial', False),
        "p4": {
            "user": username,
            "password": p4_password,
            "client": config.get('p4', {}).get('client'),
            "port": config.get('p4', {}).get('port')
        },
        "connection": config.get('ssh', {})
    }
    
    if not spec['workspace']:
        return jsonify({"error": "workspace is required"}), 400
    
    # Get SSH config
    ssh_config = config.get('ssh', {})
    ssh_host = ssh_config.get('host') or session.get('ssh_host')
    ssh_port = ssh_config.get('port') or session.get('ssh_port', 22)
    master_host = ssh_config.get('master_host') or session.get('master_host')
    master_port = ssh_config.get('master_port') or session.get('master_port', 9090)
    python_path = ssh_config.get('python_path') or session.get('python_path', 'python3')
    
    if not ssh_host or not master_host:
        return jsonify({"error": "SSH and Master host configuration required"}), 400
    
    # Deploy agent and create job
    ssh_cfg = {
        "host": ssh_host,
        "port": ssh_port,
        "user": username,
        "password": p4_password,
        "python_path": python_path
    }
    
    try:
        bootstrapper = Bootstrapper(ssh_cfg)
        pid, agent_hint = bootstrapper.deploy_agent(
            master_host=master_host,
            master_port=master_port,
            workspace=spec['workspace'],
            agent_server=agent_server
        )
        
        found_agent = agent_server.wait_for_agent(agent_hint, timeout=10.0)
        if not found_agent:
            return jsonify({"error": "Agent failed to connect"}), 504
        
        # Normalize connection info in spec with consistent keys
        spec["connection"] = {
            "ssh_host": ssh_host,
            "ssh_port": ssh_port,
            "master_host": master_host,
            "master_port": master_port,
            "python_path": python_path
        }
        
        job_id = str(uuid.uuid4())
        state_machine.create_job(job_id, found_agent, spec, owner=username)
        
        asyncio.run_coroutine_threadsafe(
            state_machine.start_job(job_id),
            agent_server._loop
        )
        
        return jsonify({
            "job_id": job_id,
            "status": "started",
            "template_id": template_id,
            "agent_id": found_agent
        })
        
    except Exception as e:
        current_app.logger.error(f"Failed to run template {template_id}: {e}")
        return jsonify({"error": str(e)}), 500


# ============= Schedule API =============

@bp.route('/schedules', methods=['GET'])
def list_schedules():
    """List all schedules for the current user.
    
    Query params:
    - workspace: Filter by workspace
    """
    username = session.get('p4_user')
    workspace = request.args.get('workspace')
    
    schedules = scheduler_manager.list_schedules(workspace=workspace, owner=username)
    return jsonify({"schedules": schedules, "count": len(schedules)})


@bp.route('/schedules', methods=['POST'])
def create_schedule():
    """Create a new schedule.
    
    Request JSON:
    - name: Schedule name (required)
    - template_id: Template ID to run (required)
    - cron: Cron expression (required, e.g., "0 2 * * *")
    - config: Optional config overrides
    - enabled: Whether enabled (default: true)
    
    Note: workspace is automatically extracted from the template.
    """
    data = request.json
    if not data:
        return jsonify({"error": "Request body is required"}), 400
    
    username = session.get('p4_user')
    if not username:
        return jsonify({"error": "Not logged in"}), 401
    
    name = data.get('name')
    template_id = data.get('template_id')
    cron = data.get('cron')
    config = data.get('config', {})
    enabled = data.get('enabled', True)
    
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not template_id:
        return jsonify({"error": "template_id is required"}), 400
    if not cron:
        return jsonify({"error": "cron expression is required"}), 400
    
    # Get workspace from template (centralized storage, no workspace needed for lookup)
    template = template_manager.get_template(template_id, username=username)
    if not template:
        return jsonify({"error": "Template not found"}), 404
    
    workspace = template.get('config', {}).get('workspace', '')
    if not workspace:
        return jsonify({"error": "Template has no workspace configured"}), 400
    
    schedule = scheduler_manager.create_schedule(
        name=name,
        template_id=template_id,
        cron=cron,
        owner=username,
        workspace=workspace,
        config=config,
        enabled=enabled
    )
    
    if schedule:
        return jsonify({"schedule": schedule}), 201
    return jsonify({"error": "Failed to create schedule (invalid cron expression?)"}), 400


@bp.route('/schedules/<schedule_id>', methods=['GET'])
def get_schedule(schedule_id):
    """Get a schedule by ID."""
    schedule = scheduler_manager.get_schedule(schedule_id)
    if schedule:
        return jsonify(schedule)
    return jsonify({"error": "Schedule not found"}), 404


@bp.route('/schedules/<schedule_id>', methods=['PUT'])
def update_schedule(schedule_id):
    """Update a schedule.
    
    Request JSON:
    - name: New name (optional)
    - cron: New cron expression (optional)
    - config: New config (optional)
    - enabled: New enabled state (optional)
    """
    data = request.json
    if not data:
        return jsonify({"error": "Request body is required"}), 400
    
    username = session.get('p4_user')
    if not username:
        return jsonify({"error": "Not logged in"}), 401
    
    # Check ownership
    schedule = scheduler_manager.get_schedule(schedule_id)
    if not schedule:
        return jsonify({"error": "Schedule not found"}), 404
    if schedule.get('owner') != username:
        return jsonify({"error": "Permission denied"}), 403
    
    updates = {}
    if 'name' in data:
        updates['name'] = data['name']
    if 'cron' in data:
        updates['cron'] = data['cron']
    if 'config' in data:
        updates['config'] = data['config']
    if 'enabled' in data:
        updates['enabled'] = data['enabled']
    
    result = scheduler_manager.update_schedule(schedule_id, updates)
    if result:
        return jsonify({"schedule": result})
    return jsonify({"error": "Failed to update schedule"}), 400


@bp.route('/schedules/<schedule_id>', methods=['DELETE'])
def delete_schedule(schedule_id):
    """Delete a schedule."""
    username = session.get('p4_user')
    if not username:
        return jsonify({"error": "Not logged in"}), 401
    
    # Check ownership
    schedule = scheduler_manager.get_schedule(schedule_id)
    if not schedule:
        return jsonify({"error": "Schedule not found"}), 404
    if schedule.get('owner') != username:
        return jsonify({"error": "Permission denied"}), 403
    
    if scheduler_manager.delete_schedule(schedule_id):
        return jsonify({"status": "deleted"})
    return jsonify({"error": "Failed to delete schedule"}), 400


@bp.route('/schedules/<schedule_id>/enable', methods=['POST'])
def enable_schedule(schedule_id):
    """Enable a schedule."""
    if scheduler_manager.enable_schedule(schedule_id):
        return jsonify({"status": "enabled"})
    return jsonify({"error": "Failed to enable schedule"}), 400


@bp.route('/schedules/<schedule_id>/disable', methods=['POST'])
def disable_schedule(schedule_id):
    """Disable a schedule."""
    if scheduler_manager.disable_schedule(schedule_id):
        return jsonify({"status": "disabled"})
    return jsonify({"error": "Failed to disable schedule"}), 400


@bp.route('/schedules/<schedule_id>/run', methods=['POST'])
def run_schedule_now(schedule_id):
    """Run a schedule immediately (manual trigger)."""
    username = session.get('p4_user')
    if not username:
        return jsonify({"error": "Not logged in"}), 401
    
    # Check ownership
    schedule = scheduler_manager.get_schedule(schedule_id)
    if not schedule:
        return jsonify({"error": "Schedule not found"}), 404
    if schedule.get('owner') != username:
        return jsonify({"error": "Permission denied"}), 403
    
    if scheduler_manager.run_now(schedule_id):
        return jsonify({"status": "triggered"})
    return jsonify({"error": "Failed to trigger schedule"}), 400


# ============= Workspace Queue API =============

@bp.route('/queue/check', methods=['GET'])
def check_workspace_queue():
    """Check if a workspace is available or busy.
    
    Query params:
    - workspace: Workspace path to check
    """
    workspace = request.args.get('workspace')
    if not workspace:
        return jsonify({"error": "workspace parameter is required"}), 400
    
    status = workspace_queue.check_workspace(workspace)
    return jsonify(status)


@bp.route('/queue/status', methods=['GET'])
def get_queue_status():
    """Get status of all workspaces with active jobs or queues."""
    states = workspace_queue.get_all_states()
    return jsonify({"workspaces": states, "count": len(states)})


@bp.route('/queue/my-jobs', methods=['GET'])
def get_my_queued_jobs():
    """Get all queued jobs for the current user."""
    username = session.get('p4_user')
    if not username:
        return jsonify({"error": "Not logged in"}), 401
    
    jobs = workspace_queue.get_user_queued_jobs(username)
    return jsonify({"queued_jobs": jobs, "count": len(jobs)})


@bp.route('/queue/<job_id>/cancel', methods=['POST'])
def cancel_queued_job(job_id):
    """Cancel a queued job (remove from queue)."""
    data = request.json or {}
    workspace = data.get('workspace')
    
    if not workspace:
        return jsonify({"error": "workspace is required"}), 400
    
    success = workspace_queue.remove_from_queue(workspace, job_id)
    if success:
        return jsonify({"status": "removed"})
    return jsonify({"error": "Job not found in queue"}), 404


# ============= Job API =============

@bp.route('/jobs', methods=['POST'])
def create_job():
    """Create and start a new job via API
    
    Required fields in request JSON:
    - job_id: Unique job identifier
    - spec: Job specification (workspace, branch_spec, etc.)
    - ssh_config: SSH configuration (host, port, user, password, python_path)
    - master_host: Master host IP for agent callback
    - master_port: Master port for agent callback (default: 9090)
    """
    data = request.json
    job_id = data.get("job_id")
    spec = data.get("spec")
    
    if not job_id or not spec:
        return jsonify({"error": "job_id and spec are required"}), 400
    
    # SSH config must be provided in request (no longer in config.yaml)
    ssh_config = data.get("ssh_config")
    if not ssh_config:
        return jsonify({"error": "ssh_config is required (host, port, user, password, python_path)"}), 400
    
    # Master host/port must be provided
    master_host = data.get("master_host")
    master_port = data.get("master_port", 9090)
    if not master_host:
        return jsonify({"error": "master_host is required"}), 400

    try:
        # Deploy Agent
        bootstrapper = Bootstrapper(ssh_config)
        pid, agent_hint = bootstrapper.deploy_agent(
            master_host=master_host,
            master_port=master_port,
            workspace=spec.get("workspace", ""),
            agent_server=agent_server
        )
        
        # Wait for agent to connect
        found_agent_id = agent_server.wait_for_agent(agent_hint, timeout=10.0)
            
        if not found_agent_id:
            return jsonify({"error": "Agent failed to connect within timeout"}), 504

        # Get owner from request or extract from spec
        owner = data.get("owner") or spec.get("p4", {}).get("user", "unknown")
        
        job = state_machine.create_job(job_id, found_agent_id, spec, owner=owner)
        
        # Start Job
        asyncio.run_coroutine_threadsafe(
            state_machine.start_job(job_id),
            agent_server._loop
        )
        
        return jsonify({"job_id": job_id, "status": "started", "agent_id": found_agent_id, "owner": owner})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@bp.route('/jobs', methods=['GET'])
def list_jobs():
    """List jobs for current user
    
    Query params:
    - owner: Filter by owner (optional, defaults to current session user)
    - all: If true, return all jobs (admin only)
    """
    owner = request.args.get('owner') or session.get('p4_user')
    show_all = request.args.get('all', '').lower() in ('true', '1', 'yes')
    
    if show_all:
        jobs = state_machine.get_all_jobs()
    elif owner:
        jobs = state_machine.get_jobs_by_owner(owner)
    else:
        jobs = []
    
    # Sort by updated_at desc
    jobs.sort(key=lambda x: x.get('updated_at', ''), reverse=True)
    
    # Return simplified job info
    result = []
    for job in jobs:
        result.append({
            'id': job.get('job_id'),
            'owner': job.get('owner', 'unknown'),
            'stage': job.get('stage'),
            'created_at': job.get('created_at'),
            'updated_at': job.get('updated_at'),
            'workspace': job.get('spec', {}).get('workspace', ''),
            'branch_spec': job.get('spec', {}).get('branch_spec', '')
        })
    
    return jsonify({"jobs": result, "count": len(result)})

@bp.route('/jobs/<job_id>', methods=['GET'])
def get_job(job_id):
    """Get job details in JSON format"""
    job_info = state_machine.get_job_info(job_id)
    if not job_info:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job_info)

@bp.route('/jobs/<job_id>/logs', methods=['GET'])
def get_job_logs(job_id):
    """Get job logs"""
    logs = state_machine.get_job_logs(job_id)
    return jsonify(logs)

@bp.route('/jobs/<job_id>/continue', methods=['POST'])
def continue_job(job_id):
    """Manually continue job from NEEDS_RESOLVE"""
    asyncio.run_coroutine_threadsafe(
        state_machine.user_continue(job_id),
        agent_server._loop
    )
    # If form submission (Accept: text/html), redirect back to job detail page
    if 'text/html' in request.headers.get('Accept', ''):
        return redirect(url_for('job_detail', job_id=job_id))
    return jsonify({"status": "continued"})

@bp.route('/agents', methods=['GET'])
def get_agents():
    """Get connected agents"""
    return jsonify(agent_server.get_connected_agents())

@bp.route('/agents/status', methods=['GET'])
def get_agent_status():
    """Get comprehensive agent status including expected/pending connections"""
    return jsonify(agent_server.get_agent_status())


@bp.route('/jobs/<job_id>/cancel', methods=['POST'])
def cancel_job(job_id):
    """Cancel a running job"""
    try:
        asyncio.run_coroutine_threadsafe(
            state_machine.cancel_job(job_id),
            agent_server._loop
        )
        return jsonify({"status": "cancelled"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@bp.route('/jobs/<job_id>/retry', methods=['POST'])
def retry_job(job_id):
    """Retry a failed job - creates a new job with the same spec
    
    SSH/Agent config is retrieved from:
    1. Request JSON body (if provided)
    2. Flask session (fallback - same as job submission)
    """
    try:
        # Get the original job
        job = state_machine.get_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        
        # Create a new job with the same spec
        new_job_id = str(uuid.uuid4())
        spec = job.get("spec", {})
        
        if not spec:
            return jsonify({"error": "Original job has no spec"}), 400
        
        # Get SSH/Agent config: request body > original job spec > session
        data = request.json if request.is_json else {}
        orig_conn = spec.get("connection", {})
        
        ssh_host = data.get("ssh_host") or orig_conn.get("ssh_host") or session.get('ssh_host')
        ssh_port = data.get("ssh_port") or orig_conn.get("ssh_port") or session.get('ssh_port', 22)
        master_host = data.get("master_host") or orig_conn.get("master_host") or session.get('master_host')
        master_port = data.get("master_port") or orig_conn.get("master_port") or session.get('master_port', 9090)
        python_path = data.get("python_path") or orig_conn.get("python_path") or session.get('python_path', 'python3')
        
        # Get P4 credentials from session (required for SSH auth)
        p4_user = session.get('p4_user')
        p4_password = session.get('p4_password')
        
        if not ssh_host or not master_host:
            return jsonify({
                "error": "SSH/Agent settings not configured. Please go to Settings page first."
            }), 400
        
        if not p4_user or not p4_password:
            return jsonify({
                "error": "Not logged in. Please login first."
            }), 401
        
        # Build SSH config
        ssh_config = {
            "host": ssh_host,
            "port": ssh_port,
            "user": p4_user,
            "password": p4_password,
            "python_path": python_path
        }
        
        # Deploy new agent
        bootstrapper = Bootstrapper(ssh_config)
        pid, agent_hint = bootstrapper.deploy_agent(
            master_host=master_host,
            master_port=master_port,
            workspace=spec.get("workspace", ""),
            agent_server=agent_server
        )
        
        # Wait for Agent to connect back (critical step!)
        found_agent = agent_server.wait_for_agent(agent_hint, timeout=10.0)
        
        if not found_agent:
            return jsonify({
                "error": f"Agent failed to connect. PID: {pid}, Hint: {agent_hint}. Check Settings and network connectivity."
            }), 504
        
        # Update spec.connection with current SSH settings (in case they changed)
        spec["connection"] = {
            "ssh_host": ssh_host,
            "ssh_port": ssh_port,
            "master_host": master_host,
            "master_port": master_port,
            "python_path": python_path
        }
        
        # Create and start the new job with correct agent_id
        # Preserve the original job's owner or use current user
        owner = job.get("owner") or p4_user
        state_machine.create_job(new_job_id, found_agent, spec, owner=owner)
        asyncio.run_coroutine_threadsafe(
            state_machine.start_job(new_job_id),
            agent_server._loop
        )
        
        return jsonify({
            "status": "success", 
            "new_job_id": new_job_id,
            "agent_id": found_agent,
            "original_job_id": job_id,
            "owner": owner
        })
    except Exception as e:
        current_app.logger.error(f"Retry job {job_id} failed: {e}")
        return jsonify({"error": str(e)}), 500

@bp.route('/jobs/<job_id>/heartbeat', methods=['GET'])
def get_job_heartbeat(job_id):
    """Get agent heartbeat status for a job"""
    job = state_machine.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    
    agent_id = job.get("agent_id")
    if not agent_id:
        return jsonify({"connected": False, "agent_id": None})
    
    agent = agent_server.agents.get(agent_id)
    if agent:
        return jsonify({
            "connected": True,
            "agent_id": agent_id,
            "last_heartbeat": agent.last_heartbeat.isoformat() if hasattr(agent, 'last_heartbeat') else None,
            "hostname": agent.hostname if hasattr(agent, 'hostname') else None
        })
    else:
        return jsonify({"connected": False, "agent_id": agent_id})

@bp.route('/jobs/<job_id>/process_status', methods=['GET'])
def get_job_process_status(job_id):
    """Get process status for a job"""
    job = state_machine.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    
    agent_id = job.get("agent_id")
    
    # Debug info
    connected_agents = list(agent_server.agents.keys())
    current_app.logger.info(f"Job {job_id} agent_id: {agent_id}, connected agents: {connected_agents}")
    
    if not agent_id:
        return jsonify({
            "processes": job.get("pids", {}),
            "agent_connected": False,
            "current_stage": job.get("stage"),
            "debug": {
                "reason": "no_agent_id",
                "connected_agents": connected_agents
            }
        })
    
    agent = agent_server.agents.get(agent_id)
    if not agent:
        return jsonify({
            "processes": job.get("pids", {}),
            "agent_connected": False,
            "current_stage": job.get("stage"),
            "debug": {
                "reason": "agent_not_found",
                "job_agent_id": agent_id,
                "connected_agents": connected_agents
            }
        })
    
    # Agent is connected
    return jsonify({
        "processes": job.get("pids", {}),
        "agent_connected": True,
        "current_stage": job.get("stage"),
        "current_cmd_id": job.get("current_cmd_id"),
        "agent_id": agent_id
    })

