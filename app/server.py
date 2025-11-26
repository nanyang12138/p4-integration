"""
Flask Server Routes (Refactored for Architecture v2)
Adapts existing UI routes to use JobStateMachine.
"""
from flask import render_template, request, redirect, url_for, jsonify, current_app, send_file, session
import uuid
import os
from datetime import datetime
from functools import wraps

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'p4_user' not in session:
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def register_routes(app, state_machine):
    
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            session['p4_user'] = request.form.get('p4_user')
            session['p4_password'] = request.form.get('p4_password')
            current_app.logger.info(f"Login: User={session['p4_user']}, Password length={len(session.get('p4_password', ''))}")
            return redirect(url_for('admin_dashboard'))
        return render_template('login.html')

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))
    
    @app.route('/settings', methods=['GET', 'POST'])
    @login_required
    def settings():
        message = None
        message_type = None
        
        if request.method == 'POST':
            # Save SSH/Agent settings to session
            session['ssh_host'] = request.form.get('ssh_host', '').strip()
            session['ssh_port'] = int(request.form.get('ssh_port', 22) or 22)
            session['master_host'] = request.form.get('master_host', '').strip()
            session['master_port'] = int(request.form.get('master_port', 9090) or 9090)
            session['python_path'] = request.form.get('python_path', 'python3').strip() or 'python3'
            
            current_app.logger.info(f"Settings saved: ssh_host={session['ssh_host']}, master_host={session['master_host']}")
            
            message = "Settings saved successfully!"
            message_type = "success"
        
        return render_template('settings.html', message=message, message_type=message_type)
    
    @app.route('/')
    @login_required
    def index():
        return redirect(url_for('admin_dashboard'))

    @app.route('/admin')
    @login_required
    def admin_dashboard():
        # Fetch all jobs from state_machine
        jobs = list(state_machine.jobs.values())
        # Sort by updated_at desc
        jobs.sort(key=lambda x: x['updated_at'], reverse=True)
        return render_template('admin.html', jobs=jobs)

    @app.route('/admin/submit', methods=['GET', 'POST'])
    @login_required
    def admin_submit():
        if request.method == 'POST':
            # P4 Configuration from form and session
            p4_user = session.get('p4_user')
            p4_password = session.get('p4_password')
            p4_client = request.form.get('p4_client')
            p4_port = request.form.get('p4_port')
            workspace = request.form.get('workspace', '').strip()
            
            current_app.logger.info(f"Submit: p4_user={p4_user}, p4_password_len={len(p4_password) if p4_password else 0}, p4_client={p4_client}, p4_port={p4_port}")
            
            if not all([p4_user, p4_password, p4_client, p4_port]):
                return render_template('error.html',
                    error_type='warning',
                    error_title='Missing P4 Configuration',
                    error_subtitle='Required fields are not filled',
                    error_message='Please provide P4 User, Password, Client, and Port to submit a job.',
                    causes=[
                        'You may not be logged in properly',
                        'P4 Client or P4 Port fields are empty in the form'
                    ],
                    action_url='/admin/submit',
                    action_text='Go Back to Form'
                ), 400
            
            if not workspace:
                return render_template('error.html',
                    error_type='warning',
                    error_title='Missing Workspace',
                    error_subtitle='Workspace path is required',
                    error_message='Please provide the workspace path on the remote machine.',
                    action_url='/admin/submit',
                    action_text='Go Back to Form'
                ), 400

            # SSH/Agent configuration from session
            ssh_host = session.get('ssh_host')
            ssh_port = session.get('ssh_port', 22)
            master_host = session.get('master_host')
            master_port = session.get('master_port', 9090)
            python_path = session.get('python_path', 'python3')
            
            if not ssh_host or not master_host:
                return render_template('error.html',
                    error_type='warning',
                    error_title='Settings Required',
                    error_subtitle='SSH and Agent configuration is missing',
                    error_message='Before submitting a job, you need to configure SSH Host and Master Host in the Settings page.',
                    causes=[
                        'SSH Host is not configured',
                        'Master Host (Agent callback address) is not configured'
                    ],
                    action_url='/settings',
                    action_text='Go to Settings'
                ), 400

            spec = {
                "workspace": workspace,
                "branch_spec": request.form.get("branch_spec"),
                "changelist": request.form.get("changelist"),
                "path": request.form.get("path", ""),
                "description": request.form.get("description", ""),
                "trial": request.form.get("trial") == "true",
                "p4": {
                    "user": p4_user,
                    "password": p4_password,
                    "client": p4_client,
                    "port": p4_port
                },
                # Connection info for display in job details
                "connection": {
                    "ssh_host": ssh_host,
                    "ssh_port": ssh_port,
                    "master_host": master_host,
                    "master_port": master_port,
                    "python_path": python_path
                }
            }
            
            job_id = str(uuid.uuid4())
            
            # Build SSH config from session
            ssh_config = {
                "host": ssh_host,
                "port": ssh_port,
                "user": p4_user,  # Use P4 user as SSH user
                "password": p4_password,  # Use P4 password as SSH password
                "python_path": python_path
            }
                
            from app.master.bootstrapper import Bootstrapper
            try:
                # Deploy Agent
                bootstrapper = Bootstrapper(ssh_config)
                pid, agent_hint = bootstrapper.deploy_agent(
                    master_host=master_host,
                    master_port=master_port,
                    workspace=spec["workspace"],
                    agent_server=state_machine.agent_server
                )
                
                # Wait for Agent to connect back
                found_agent = state_machine.agent_server.wait_for_agent(agent_hint, timeout=10.0)
                
                if not found_agent:
                    # Provide detailed error information using template
                    return render_template('error.html',
                        error_type='error',
                        error_title='Agent Connection Failed',
                        error_subtitle=f'Failed to connect to agent on {ssh_host}',
                        error_message=f'The agent was deployed to <strong>{ssh_host}</strong> but failed to connect back to Master at <strong>{master_host}:{master_port}</strong>.',
                        causes=[
                            f'Firewall blocking port <code>{master_port}</code> on Master',
                            f'Master IP <code>{master_host}</code> is not reachable from <code>{ssh_host}</code>',
                            f'Python 3 is not installed or not found at <code>{python_path}</code>',
                            f'SSH deployment failed (Agent PID: <code>{pid}</code>)'
                        ],
                        debug_steps=[
                            {
                                'description': 'Verify agent process is running on remote machine',
                                'command': f'ssh {p4_user}@{ssh_host} "ps aux | grep -E \'python.*agent_core|{pid}\'"'
                            },
                            {
                                'description': 'Test network connectivity from remote to Master',
                                'command': f'ssh {p4_user}@{ssh_host} "nc -zv {master_host} {master_port}"'
                            },
                            {
                                'description': 'Check if Master is listening on the port',
                                'command': f'netstat -tlnp | grep {master_port}'
                            },
                            {
                                'description': 'Check agent startup logs on remote machine',
                                'command': f'ssh {p4_user}@{ssh_host} "cat /tmp/p4_agent_boot_*.log | tail -50"'
                            }
                        ],
                        technical_details=f'SSH Host: {ssh_host}:{ssh_port}\nMaster Host: {master_host}:{master_port}\nPython Path: {python_path}\nAgent PID: {pid}\nAgent Hint: {agent_hint}',
                        retry_url='/admin/submit'
                    ), 500
                    
                state_machine.create_job(job_id, found_agent, spec)
                
                import asyncio
                asyncio.run_coroutine_threadsafe(
                    state_machine.start_job(job_id),
                    state_machine.agent_server._loop
                )
                
                return redirect(url_for('job_detail', job_id=job_id))
                
            except Exception as e:
                current_app.logger.error(f"Failed to start job: {e}")
                error_str = str(e)
                
                # Determine error type and provide helpful context
                causes = []
                debug_steps = []
                
                if 'SSH' in error_str or 'Authentication' in error_str or 'timeout' in error_str.lower():
                    # SSH connection error
                    causes = [
                        f'SSH authentication failed for user <code>{p4_user}</code>',
                        f'SSH host <code>{ssh_host}</code> is unreachable or port <code>{ssh_port}</code> is blocked',
                        'Network timeout - the remote machine may be slow to respond',
                        'Incorrect password or the user does not have SSH access'
                    ]
                    debug_steps = [
                        {
                            'description': 'Test SSH connection manually',
                            'command': f'ssh -p {ssh_port} {p4_user}@{ssh_host}'
                        },
                        {
                            'description': 'Check if SSH port is open',
                            'command': f'nc -zv {ssh_host} {ssh_port}'
                        },
                        {
                            'description': 'Verify your credentials are correct in Settings'
                        }
                    ]
                elif 'Agent' in error_str or 'PID' in error_str:
                    # Agent startup error
                    causes = [
                        f'Python is not installed or not found at <code>{python_path}</code> on remote machine',
                        'The agent script failed to start',
                        'Permission issues on the remote machine'
                    ]
                    debug_steps = [
                        {
                            'description': 'Check Python availability on remote machine',
                            'command': f'ssh {p4_user}@{ssh_host} "which python3 && python3 --version"'
                        },
                        {
                            'description': 'Check agent boot logs',
                            'command': f'ssh {p4_user}@{ssh_host} "cat /tmp/p4_agent_boot_*.log | tail -50"'
                        }
                    ]
                else:
                    # Generic error
                    causes = [
                        'An unexpected error occurred during job initialization',
                        'Check the technical details below for more information'
                    ]
                
                return render_template('error.html',
                    error_type='error',
                    error_title='Failed to Start Job',
                    error_subtitle=f'Error connecting to {ssh_host}' if ssh_host else 'An unexpected error occurred',
                    error_message=f'The job could not be started. Please check the details below and try again.',
                    causes=causes,
                    debug_steps=debug_steps if debug_steps else None,
                    technical_details=error_str,
                    retry_url='/admin/submit'
                ), 500
                
        return render_template('admin_submit.html')

    @app.route('/jobs/<job_id>')
    @login_required
    def job_detail(job_id):
        job = state_machine.get_job(job_id)
        if not job:
            return render_template('error.html',
                error_type='warning',
                error_title='Job Not Found',
                error_subtitle=f'Job ID: {job_id[:8]}...',
                error_message='The requested job could not be found. It may have been deleted or the ID is incorrect.',
                action_url='/admin',
                action_text='Back to Dashboard'
            ), 404
        
        # Debug: log agent info
        agent_id = job.get("agent_id")
        connected_agents = list(state_machine.agent_server.agents.keys())
        app.logger.info(f"Job {job_id} detail page - agent_id: {agent_id}, connected: {connected_agents}")
        
        return render_template('job_detail.html', job=job)

    @app.route('/jobs/<job_id>/logs/download')
    @login_required
    def download_job_logs(job_id):
        """Download job logs as txt file"""
        paths = state_machine.get_log_file_path(job_id)
        txt_path = paths.get("txt")
        if txt_path and os.path.exists(txt_path):
            return send_file(
                txt_path, 
                as_attachment=True, 
                download_name=f"job_{job_id[:8]}_logs.txt"
            )
        return render_template('error.html',
            error_type='warning',
            error_title='Log File Not Found',
            error_subtitle=f'Job ID: {job_id[:8]}...',
            error_message='The log file for this job could not be found.',
            action_url=f'/jobs/{job_id}',
            action_text='Back to Job Details'
        ), 404
