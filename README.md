# P4 Integration Service

> Automated Perforce integration service with Master-Agent architecture for distributed execution

![Python](https://img.shields.io/badge/python-3.10+-blue.svg)
![Flask](https://img.shields.io/badge/flask-2.2.5-green.svg)

A distributed Perforce integration service using Master-Agent architecture with event-driven state machine for reliable, scalable P4 operations.

## Features

- **Master-Agent Architecture** - Distributed execution with central control
- **Event-Driven State Machine** - Non-blocking, asynchronous job execution
- **Auto-Deploy** - Master automatically deploys Agents via SSH
- **Auto-Detect Master** - Hostname auto-detected on startup, no hardcoding needed
- **Real-time Logs** - Live log streaming via TCP socket
- **Template System** - Save and reuse job configurations (global & private)
- **Scheduled Jobs** - Cron-based automated execution with stored credentials
- **Smart Conflict Resolution** - Auto-merge with manual fallback
- **P4 Login Validation** - Credentials validated against P4 server (without affecting tickets)
- **Modern Web UI** - Clean, responsive interface with Tailwind CSS

## Architecture

```
+-----------------+          +-----------------+
|  Master (Web)   |          |  Agent (Remote) |
|                 |          |                 |
|  Flask UI :5000 |          |   P4 CLI        |
|  State Machine  |<--TCP--->|   Executor      |
|  Agent Server   |   9090   |   agent_core.py |
|  Scheduler      |          |                 |
+-----------------+          +-----------------+
```

1. User creates a job via Web UI
2. Master deploys `agent_core.py` to remote machine via SSH (Bootstrapper)
3. Agent connects back to Master on TCP port 9090
4. Master sends P4 commands, Agent executes and streams logs back
5. On completion/failure/cancel, Agent is shut down and workspace is cleaned up

## Quick Start

### 1. Install Dependencies

```bash
cd /path/to/p4-integration
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Start the Server

```bash
python wsgi.py
```

Server starts on `http://0.0.0.0:5000`. The master hostname is auto-detected and saved to `data/master_host`.

To run in the background (persists after closing terminal):

```bash
nohup python wsgi.py > /tmp/p4_integ_server.log 2>&1 &
```

### 3. Access the Web UI

If you are on the same machine as the server:

```
http://<server-hostname>:5000
```

Log in with your P4 username and password.

## Quick Connect Script

A `connect.sh` script is provided for easy access. It auto-detects whether you are on the server or a remote machine:

```bash
/path/to/p4-integration/connect.sh
```

- **On the server**: Opens Firefox directly to the Web UI
- **On a remote machine**: Sets up an SSH tunnel and opens Firefox to `http://localhost:5000`

The script reads the master hostname from `data/master_host` (written by `wsgi.py` on startup), so no hardcoded hostnames are needed.

## Sharing with Others

### Option 1: Direct access (same machine)

If colleagues log into the same server, they just run:

```bash
/path/to/p4-integration/connect.sh
```

### Option 2: SSH tunnel (different machine)

If colleagues are on a different machine, the `connect.sh` script automatically sets up an SSH tunnel. Alternatively, they can do it manually:

```bash
ssh -f -N -L 5000:localhost:5000 <username>@<server-hostname>
```

Then open `http://localhost:5000` in the browser.

**Tip**: Add this to `~/.ssh/config` for convenience:

```
Host p4tool
    HostName <server-hostname>
    User your_username
    LocalForward 5000 localhost:5000
```

Then just `ssh p4tool` and open `http://localhost:5000`.

## Job Workflow

### State Machine Flow

```
GET_LATEST_CL -> SYNC -> INTEGRATE -> RESOLVE_PASS_1 -> RESOLVE_CHECK
                                                              |
                                              (no conflicts)  |  (conflicts found)
                                                    v         v
                                              PRE_SUBMIT   NEEDS_RESOLVE
                                                    |         |
                                                    v    (user resolves manually,
                                                  SHELVE  then continues)
                                                    |
                                                    v
                                                 P4PUSH -> DONE

On failure at any stage:   -> CLEANUP (p4 revert) -> ERROR
On cancel:                 -> kill process group -> wait -> CLEANUP -> ERROR
```

### Stage Details

| Stage | Description |
|-------|-------------|
| GET_LATEST_CL | Fetch latest submitted changelist from source |
| SYNC | Update workspace to latest (`p4w sync_all`) |
| INTEGRATE | Perform P4 integration (`p4 integrate`) |
| RESOLVE_PASS_1 | Auto-merge (`p4 resolve -am`) |
| RESOLVE_CHECK | Check for remaining conflicts (`p4 resolve -n`) |
| NEEDS_RESOLVE | Wait for manual conflict resolution |
| PRE_SUBMIT | Run pre-submit hooks (optional) |
| SHELVE | Create changelist, shelve files, run name_check |
| P4PUSH | Push changelist to target (`p4push`) |
| CLEANUP | Revert opened files on failure/cancel |
| DONE | Job completed successfully |
| ERROR | Job failed |

## Web Interface

| Page | URL | Description |
|------|-----|-------------|
| Dashboard | `/admin` | View all jobs with running/completed/failed counts |
| New Job | `/admin/submit` | Create integration job (SSH config included in form) |
| Job Detail | `/jobs/<id>` | View logs, resolve conflicts, cancel/retry |
| Templates | `/templates` | Save and reuse job configurations (global & private) |
| Schedules | `/schedules` | Cron-based automated job execution with Run Now |
| Settings | `/settings` | SSH/Agent connection defaults |

## API Reference

### Jobs
- `POST /api/jobs` - Create new job
- `GET /api/jobs` - List jobs
- `GET /api/jobs/<id>` - Get job details
- `GET /api/jobs/<id>/logs` - Get job logs
- `POST /api/jobs/<id>/continue` - Continue after manual resolve
- `POST /api/jobs/<id>/cancel` - Cancel running job
- `POST /api/jobs/<id>/retry` - Retry failed job
- `GET /api/jobs/<id>/heartbeat` - Check agent connection health
- `GET /api/jobs/<id>/process_status` - Get running process info

### Templates
- `GET /api/templates` - List templates
- `POST /api/templates` - Create template
- `PUT /api/templates/<id>` - Update template (supports type change: private/global)
- `DELETE /api/templates/<id>` - Delete template
- `POST /api/templates/<id>/run` - Run job from template

### Schedules
- `GET /api/schedules` - List schedules
- `POST /api/schedules` - Create schedule (auto-saves P4 credentials)
- `PUT /api/schedules/<id>` - Update schedule
- `DELETE /api/schedules/<id>` - Delete schedule
- `POST /api/schedules/<id>/run` - Run schedule immediately (returns job_id)
- `POST /api/schedules/<id>/enable` - Enable schedule
- `POST /api/schedules/<id>/disable` - Disable schedule

### Agents
- `GET /api/agents` - List connected agents
- `GET /api/agents/status` - Agent connection status

## Configuration

Configuration is loaded from `config.yaml` (optional) and environment variables:

| Setting | Env Variable | Default | Description |
|---------|-------------|---------|-------------|
| P4 Port | `P4PORT` | `atlvp4p01.amd.com:1677` | Perforce server address |
| P4 User | `P4USER` | - | Perforce username |
| Log Level | `LOG_LEVEL` | INFO | Logging level |
| Data Dir | `P4_INTEG_DATA_DIR` | `./data` | Data storage directory |
| Flask Secret | `FLASK_SECRET_KEY` | dev key | Session encryption key |
| Server Host | `FLASK_HOST` | 0.0.0.0 | Flask bind address |
| Server Port | `PORT` | 5000 | Flask port |

**Note**: Do not use the `HOST` env variable for Flask binding -- it conflicts with the system hostname on many Linux distributions. Use `FLASK_HOST` instead.

## Project Structure

```
p4-integration/
├── app/
│   ├── agent/
│   │   └── agent_core.py        # Remote agent (deployed via SSH)
│   ├── master/
│   │   ├── agent_server.py      # TCP server for agent connections
│   │   ├── job_state_machine.py # Core state machine logic
│   │   ├── bootstrapper.py      # SSH agent deployment
│   │   └── workspace_queue.py   # Workspace concurrency control
│   ├── models/
│   │   └── template.py          # Template management
│   ├── scheduler/
│   │   └── scheduler.py         # Cron-based scheduling
│   ├── templates/               # HTML templates (Jinja2 + Tailwind)
│   ├── api.py                   # REST API endpoints
│   ├── server.py                # Flask UI routes
│   ├── config.py                # Configuration loading
│   └── __init__.py              # App factory + job runner registration
├── data/
│   ├── master_host              # Auto-generated: current master hostname
│   ├── templates/               # Stored templates (global/private)
│   └── logs/                    # Job logs
├── wsgi.py                      # Entry point
├── connect.sh                   # Quick connect script for users
├── requirements.txt             # Python dependencies
└── README.md
```

## Troubleshooting

### Agent Connection Failed

1. Check that port 9090 is accessible from the remote machine:
   ```bash
   ssh user@remote "nc -zv <master-hostname> 9090"
   ```

2. Verify the agent process is running:
   ```bash
   ssh user@remote "ps aux | grep agent_core"
   ```

3. Check agent boot logs:
   ```bash
   ssh user@remote "cat /tmp/p4_agent_boot_*.log | tail -50"
   ```

### Cannot Access Web UI from Another Machine

1. Verify the server is running: `ps aux | grep wsgi.py`
2. Check Flask is binding to `0.0.0.0` (not a specific hostname). If it shows `Running on http://<hostname>:5000` instead of `http://0.0.0.0:5000`, check that `FLASK_HOST` is not set, or unset the system `HOST` variable.
3. Test port connectivity: `nc -zv <hostname> 5000`
4. If blocked by firewall, use `connect.sh` or SSH port forwarding (see "Sharing with Others" above)
5. When using SSH tunnel, make sure `data/master_host` exists (start `wsgi.py` first)

### P4 Login Issues

- The tool validates P4 credentials on login using `p4 login -p` (print-only, does not modify your P4 tickets)
- If you get authentication errors, verify your password: `p4 -p <P4PORT> -u <user> login -s`
- Default P4PORT is `atlvp4p01.amd.com:1677`, configurable via `P4PORT` env var or `config.yaml`

### Cancel Not Working

- Cancel kills the entire process group (shell + all child processes)
- The job waits for the process to die before running cleanup (`p4 revert`)
- If a job progresses past the stage you wanted to cancel, it means the stages completed before the cancel signal arrived

## License

MIT
