"""
Flask Server Routes (Refactored for Architecture v2)
Adapts existing UI routes to use JobStateMachine.
"""
from flask import render_template, request, redirect, url_for, jsonify, current_app
import uuid
from datetime import datetime

def register_routes(app, state_machine):
    
    @app.route('/')
    def index():
        return redirect(url_for('admin_dashboard'))

    @app.route('/admin')
    def admin_dashboard():
        # Fetch all jobs from state_machine
        jobs = list(state_machine.jobs.values())
        # Sort by updated_at desc
        jobs.sort(key=lambda x: x['updated_at'], reverse=True)
        return render_template('admin.html', jobs=jobs)

    @app.route('/admin/submit', methods=['GET', 'POST'])
    def admin_submit():
        if request.method == 'POST':
            # Form submission
            spec = {
                "workspace": request.form.get("workspace"),
                "branch_spec": request.form.get("branch_spec"),
                "changelist": request.form.get("changelist"),
                "path": request.form.get("path", ""),
                "init_script": request.form.get("init_script", ""),
                "description": request.form.get("description", "")
            }
            
            job_id = str(uuid.uuid4())
            
            # Get Config
            app_config = current_app.config["APP_CONFIG"]
            ssh_config = app_config.get("ssh")
            master_host = app_config.get("agent", {}).get("master_host", "127.0.0.1")
            
            if not ssh_config:
                return "SSH Config missing", 500
                
            from app.master.bootstrapper import Bootstrapper
            try:
                # Deploy Agent
                bootstrapper = Bootstrapper(ssh_config)
                pid, agent_hint = bootstrapper.deploy_agent(
                    master_host=master_host,
                    master_port=9090,
                    workspace=spec["workspace"],
                    agent_server=state_machine.agent_server
                )
                
                # Wait for Agent to connect back
                found_agent = state_machine.agent_server.wait_for_agent(agent_hint, timeout=10.0)
                
                if not found_agent:
                    # Provide detailed error information
                    agent_config = app_config.get("agent", {})
                    error_msg = f"""<div class="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded">
                        <h3 class="font-bold mb-2">🚫 Agent Connection Failed!</h3>
                        <p class="mb-3">The agent on <code>{ssh_config['host']}</code> failed to connect back to Master.</p>
                        
                        <div class="bg-white rounded p-3 mb-3">
                            <h4 class="font-semibold mb-2">Possible Causes:</h4>
                            <ul class="list-disc ml-5 space-y-1 text-sm">
                                <li>Firewall blocking port <code>{agent_config.get('master_port', 9090)}</code></li>
                                <li>Master IP <code>{agent_config.get('master_host', '127.0.0.1')}</code> unreachable from <code>{ssh_config['host']}</code></li>
                                <li>Python 3 not installed on remote machine</li>
                                <li>SSH deployment failed (check Agent PID: {pid})</li>
                            </ul>
                        </div>
                        
                        <div class="bg-white rounded p-3">
                            <h4 class="font-semibold mb-2">Debug Steps:</h4>
                            <ol class="list-decimal ml-5 space-y-1 text-sm">
                                <li>Verify agent is running:<br>
                                    <code class="bg-gray-100 px-1">ssh {ssh_config['user']}@{ssh_config['host']} "ps aux | grep -E 'python.*agent_core|{pid}'"</code>
                                </li>
                                <li>Test connectivity from remote:<br>
                                    <code class="bg-gray-100 px-1">ssh {ssh_config['user']}@{ssh_config['host']} "nc -zv {agent_config.get('master_host', '127.0.0.1')} {agent_config.get('master_port', 9090)}"</code>
                                </li>
                                <li>Check Master is listening:<br>
                                    <code class="bg-gray-100 px-1">netstat -tlnp | grep {agent_config.get('master_port', 9090)}</code>
                                </li>
                            </ol>
                        </div>
                    </div>"""
                    return error_msg, 500
                    
                state_machine.create_job(job_id, found_agent, spec)
                
                import asyncio
                asyncio.run_coroutine_threadsafe(
                    state_machine.start_job(job_id),
                    state_machine.agent_server._loop
                )
                
                return redirect(url_for('job_detail', job_id=job_id))
                
            except Exception as e:
                return f"Failed to start job: {e}", 500
                
        return render_template('admin_submit.html')

    @app.route('/jobs/<job_id>')
    def job_detail(job_id):
        job = state_machine.get_job(job_id)
        if not job:
            return "Job not found", 404
        return render_template('job_detail.html', job=job)
