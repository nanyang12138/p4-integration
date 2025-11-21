from flask import Blueprint, jsonify, request, current_app
from app import state_machine, agent_server
from app.master.bootstrapper import Bootstrapper
import asyncio

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
    """Get job status"""
    job = state_machine.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)

@bp.route('/jobs/<job_id>/logs', methods=['GET'])
def get_job_logs(job_id):
    """Get job logs"""
    logs = state_machine.get_job_logs(job_id)
    return jsonify({"logs": logs})

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

