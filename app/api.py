from flask import Blueprint, jsonify, request, current_app, redirect, url_for, session
from app import state_machine, agent_server
from app.master.bootstrapper import Bootstrapper
import asyncio
import uuid

bp = Blueprint('api', __name__, url_prefix='/api')

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

        job = state_machine.create_job(job_id, found_agent_id, spec)
        
        # Start Job
        asyncio.run_coroutine_threadsafe(
            state_machine.start_job(job_id),
            agent_server._loop
        )
        
        return jsonify({"job_id": job_id, "status": "started", "agent_id": found_agent_id})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
        
        # Get SSH/Agent config from request body or session
        data = request.json if request.is_json else {}
        
        # Try request body first, then fall back to session
        ssh_host = data.get("ssh_host") or session.get('ssh_host')
        ssh_port = data.get("ssh_port") or session.get('ssh_port', 22)
        master_host = data.get("master_host") or session.get('master_host')
        master_port = data.get("master_port") or session.get('master_port', 9090)
        python_path = data.get("python_path") or session.get('python_path', 'python3')
        
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
        
        # Create and start the new job with correct agent_id
        state_machine.create_job(new_job_id, found_agent, spec)
        asyncio.run_coroutine_threadsafe(
            state_machine.start_job(new_job_id),
            agent_server._loop
        )
        
        return jsonify({
            "status": "success", 
            "new_job_id": new_job_id,
            "agent_id": found_agent,
            "original_job_id": job_id
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

