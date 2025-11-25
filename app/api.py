from flask import Blueprint, jsonify, request, current_app
from app import state_machine, agent_server
from app.master.bootstrapper import Bootstrapper
import asyncio
import uuid

bp = Blueprint('api', __name__, url_prefix='/api')

@bp.route('/jobs', methods=['POST'])
def create_job():
    """Create and start a new job"""
    data = request.json
    job_id = data["job_id"]
    spec = data["spec"]
    
    # Get SSH config from request or global config
    ssh_config = data.get("ssh_config")
    if not ssh_config:
        app_config = current_app.config["APP_CONFIG"]
        ssh_config = app_config.get("ssh")
    
    if not ssh_config:
        return jsonify({"error": "SSH config missing"}), 400
        
    master_host = data.get("master_host")
    if not master_host:
        # Try to guess master host IP or use config
        # For now, default to config or require it
        app_config = current_app.config["APP_CONFIG"]
        master_host = app_config.get("agent", {}).get("master_host", "127.0.0.1")

    try:
        # Deploy Agent
        bootstrapper = Bootstrapper(ssh_config)
        # Note: bootstrapper returns PID, but we don't strictly need it for logic, 
        # just to know it started. Agent will register itself.
        bootstrapper.deploy_agent(
            master_host=master_host,
            master_port=9090,
            workspace=spec["workspace"]
        )
        
        # Create Job
        # We don't know the exact agent_id yet (it registers as hostname:ip), 
        # but we can try to predict it or wait for it.
        # BETTER: Wait for any agent to connect from that host?
        # OR: Just create the job with pending agent_id, and let start_job bind it?
        # For simplicity in this phase:
        # We wait for the agent to connect.
        
        # Hack: we don't know exact agent_id key used by server until it connects.
        # But we know the hostname from ssh_config.
        expected_hostname = ssh_config["host"] # Hostname used for SSH might match
        
        # Poll for agent connection
        found_agent_id = None
        for _ in range(15):
            agents = agent_server.get_connected_agents()
            # Check if any agent matches our expected host or IP
            # Simple check: most recently connected?
            # Or check hostname in agent info
            for aid, info in agents.items():
                if info.get("hostname") == expected_hostname or \
                   info.get("ip") == expected_hostname: # approximate check
                    found_agent_id = aid
                    break
            if found_agent_id:
                break
            asyncio.run(asyncio.sleep(1))
            
        if not found_agent_id:
            # Fallback: if running locally for dev, maybe it's already connected?
            pass
            
        if not found_agent_id:
             # If we can't find it, we can't send commands.
             # But maybe it's slow to start. 
             # We can create the job anyway and queue it?
             # Current StateMachine expects agent_id.
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
    """Retry a failed job - creates a new job with the same spec"""
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
        
        # Get SSH config
        app_config = current_app.config["APP_CONFIG"]
        ssh_config = app_config.get("ssh")
        master_host = app_config.get("agent", {}).get("master_host", "127.0.0.1")
        
        if not ssh_config:
            return jsonify({"error": "SSH config missing"}), 500
        
        # Deploy new agent
        bootstrapper = Bootstrapper(ssh_config)
        pid, agent_hint = bootstrapper.deploy_agent(
            master_host=master_host,
            master_port=9090,
            workspace=spec.get("workspace", ""),
            agent_server=agent_server
        )
        
        # Wait for Agent to connect back (critical step!)
        found_agent = agent_server.wait_for_agent(agent_hint, timeout=10.0)
        
        if not found_agent:
            return jsonify({
                "error": f"Agent failed to connect. PID: {pid}, Hint: {agent_hint}"
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

