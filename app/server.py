"""
Flask Server Routes (Refactored for Architecture v2)
Adapts existing UI routes to use JobStateMachine.
"""
from flask import render_template, request, redirect, url_for, jsonify, current_app, send_file, session
from app.models.template import template_manager
from app import workspace_queue, scheduler_manager
import uuid
import os
import subprocess
from datetime import datetime
from functools import wraps

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'p4_user' not in session:
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def _validate_p4_credentials(p4_user: str, p4_password: str) -> tuple:
    """Validate P4 credentials by running 'p4 login' against the configured P4 server.
    
    Returns:
        (is_valid: bool, error_message: str or None)
        - (True, None) on success
        - (False, error_msg) on authentication failure or system error
    """
    cfg = current_app.config.get("APP_CONFIG", {})
    p4_cfg = cfg.get("p4", {})
    p4_bin = p4_cfg.get("bin", "/tool/pandora64/bin/p4")
    p4_port = p4_cfg.get("port", "") or "atlvp4p01.amd.com:1677"
    
    try:
        result = subprocess.run(
            [p4_bin, "-p", p4_port, "-u", p4_user, "login", "-p"],
            input=(p4_password + "\n").encode(),
            capture_output=True,
            timeout=10
        )
        
        if result.returncode == 0:
            current_app.logger.info(f"P4 login validation succeeded for user {p4_user}")
            return (True, None)
        else:
            # Parse P4 error — show a clean message to user, log full details
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            current_app.logger.warning(f"P4 login validation failed for user {p4_user}: {stderr}")
            
            # Map common P4 errors to user-friendly messages
            stderr_lower = stderr.lower()
            if "password invalid" in stderr_lower or "invalid credentials" in stderr_lower:
                user_msg = "Password incorrect. Please check your AMD password."
            elif "doesn't exist" in stderr_lower or "does not exist" in stderr_lower:
                user_msg = f"User '{p4_user}' not found on P4 server."
            elif "connect to server failed" in stderr_lower or "connection refused" in stderr_lower:
                user_msg = "Cannot connect to P4 server. Please try again later."
            else:
                # Unknown error — show first meaningful line only
                first_line = stderr.split('\n')[0][:100] if stderr else "Authentication failed"
                user_msg = first_line
            
            return (False, user_msg)
            
    except FileNotFoundError:
        current_app.logger.error(f"P4 binary not found at {p4_bin}")
        return (False, f"P4 binary not found at {p4_bin}. Please verify the installation.")
    except subprocess.TimeoutExpired:
        current_app.logger.error(f"P4 login timed out connecting to {p4_port}")
        return (False, "P4 server connection timed out. Please check the server status and try again.")
    except Exception as e:
        current_app.logger.error(f"Unexpected error during P4 login validation: {e}")
        return (False, f"Validation error: {e}")


def register_routes(app, state_machine):
    
    @app.context_processor
    def inject_master_host():
        """Make master_host available to all templates."""
        return {"master_host": app.config.get("MASTER_HOST", "")}
    
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            p4_user = (request.form.get('p4_user') or '').strip()
            p4_password = request.form.get('p4_password') or ''
            
            # Server-side empty check
            if not p4_user or not p4_password:
                return render_template('login.html',
                    error="Username and password are required.",
                    last_user=p4_user)
            
            # Validate credentials against P4 server
            is_valid, error_msg = _validate_p4_credentials(p4_user, p4_password)
            
            if not is_valid:
                current_app.logger.warning(f"Login rejected for user {p4_user}: {error_msg}")
                return render_template('login.html',
                    error=error_msg,
                    last_user=p4_user)
            
            # Credentials valid (or validation skipped) - store in session
            session['p4_user'] = p4_user
            session['p4_password'] = p4_password
            current_app.logger.info(f"Login: User={p4_user}, Password length={len(p4_password)}")
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
        # Fetch jobs belonging to current user
        current_user = session.get('p4_user')
        jobs = state_machine.get_jobs_by_owner(current_user)
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

            # SSH/Agent configuration from form (primary) or session (fallback)
            ssh_host = request.form.get('ssh_host', '').strip() or session.get('ssh_host')
            ssh_port = int(request.form.get('ssh_port') or session.get('ssh_port', 22) or 22)
            master_host = request.form.get('master_host', '').strip() or session.get('master_host')
            master_port = int(request.form.get('master_port') or session.get('master_port', 9090) or 9090)
            python_path = request.form.get('python_path', '').strip() or session.get('python_path', 'python3')
            
            # Save to session for future use (so Settings page shows current values)
            if ssh_host:
                session['ssh_host'] = ssh_host
            if master_host:
                session['master_host'] = master_host
            session['ssh_port'] = ssh_port
            session['master_port'] = master_port
            session['python_path'] = python_path
            
            if not ssh_host or not master_host:
                return render_template('error.html',
                    error_type='warning',
                    error_title='Connection Settings Required',
                    error_subtitle='SSH Host and Master Host are required',
                    error_message='Please expand the "SSH / Agent Connection" section in the job form and fill in SSH Host and Master Host.',
                    causes=[
                        'SSH Host is not configured — the remote machine where P4 commands run',
                        'Master Host is not configured — the IP/hostname for Agent to connect back'
                    ],
                    action_url='/admin/submit',
                    action_text='Go Back to Form'
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
                    
                # Check if workspace is available
                queue_result = workspace_queue.try_acquire(workspace, job_id, p4_user)
                
                if queue_result["acquired"]:
                    # Workspace available - start job immediately
                    state_machine.create_job(job_id, found_agent, spec, owner=p4_user)
                    
                    import asyncio
                    asyncio.run_coroutine_threadsafe(
                        state_machine.start_job(job_id),
                        state_machine.agent_server._loop
                    )
                    
                    # Save workspace to session for template lookup
                    session['last_workspace'] = workspace
                    
                    return redirect(url_for('job_detail', job_id=job_id))
                else:
                    # Workspace busy - add to queue
                    position = workspace_queue.queue_job(
                        workspace=workspace,
                        job_id=job_id,
                        owner=p4_user,
                        spec=spec
                    )
                    
                    # Create a placeholder job entry so user can track it
                    state_machine.create_job(job_id, found_agent, spec, owner=p4_user)
                    
                    # Store queue info in session for display
                    session['last_workspace'] = workspace
                    
                    # Show queue info page
                    return render_template('job_queued.html',
                        job_id=job_id,
                        position=position,
                        workspace=workspace,
                        running_job_id=queue_result.get("running_job_id"),
                        running_job_owner=queue_result.get("running_job_owner")
                    )
                
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

    # ============= Template Routes =============
    
    @app.route('/templates')
    @login_required
    def template_list():
        """List all templates"""
        username = session.get('p4_user')
        
        global_templates = template_manager.list_global_templates()
        # Private templates now use centralized storage, no workspace needed
        private_templates = template_manager.list_user_templates(username=username)
        
        return render_template('template_list.html',
            global_templates=global_templates,
            private_templates=private_templates
        )
    
    @app.route('/templates/new')
    @login_required
    def template_new():
        """Create new template form"""
        return render_template('template_edit.html', template=None)
    
    @app.route('/templates/<template_id>/edit')
    @login_required
    def template_edit(template_id):
        """Edit template form"""
        username = session.get('p4_user')
        
        # Private templates now use centralized storage, no workspace needed
        template = template_manager.get_template(template_id, username=username)
        if not template:
            return render_template('error.html',
                error_type='warning',
                error_title='Template Not Found',
                error_message='The requested template could not be found.',
                action_url='/templates',
                action_text='Back to Templates'
            ), 404
        
        return render_template('template_edit.html', template=template)

    # ============= Schedule Routes =============
    
    @app.route('/schedules')
    @login_required
    def schedule_list():
        """List all schedules"""
        username = session.get('p4_user')
        workspace = session.get('last_workspace', '')
        
        schedules = scheduler_manager.list_schedules(owner=username)
        
        return render_template('schedule_list.html', schedules=schedules)
    
    @app.route('/schedules/new')
    @login_required
    def schedule_new():
        """Create new schedule form"""
        username = session.get('p4_user')
        workspace = session.get('last_workspace', '')
        
        # Get templates for selection
        templates = template_manager.list_all_templates(workspace, username)
        
        return render_template('schedule_edit.html', schedule=None, templates=templates)
    
    @app.route('/schedules/<schedule_id>/edit')
    @login_required
    def schedule_edit(schedule_id):
        """Edit schedule form"""
        username = session.get('p4_user')
        workspace = session.get('last_workspace', '')
        
        schedule = scheduler_manager.get_schedule(schedule_id)
        if not schedule:
            return render_template('error.html',
                error_type='warning',
                error_title='Schedule Not Found',
                error_message='The requested schedule could not be found.',
                action_url='/schedules',
                action_text='Back to Schedules'
            ), 404
        
        # Check ownership
        if schedule.get('owner') != username:
            return render_template('error.html',
                error_type='error',
                error_title='Permission Denied',
                error_message='You do not have permission to edit this schedule.',
                action_url='/schedules',
                action_text='Back to Schedules'
            ), 403
        
        templates = template_manager.list_all_templates(workspace, username)
        
        return render_template('schedule_edit.html', schedule=schedule, templates=templates)
