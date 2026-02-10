# P4 Integration Service

> Automated Perforce integration service with Master-Agent architecture for distributed execution

![Python](https://img.shields.io/badge/python-3.10+-blue.svg)
![Flask](https://img.shields.io/badge/flask-2.2.5-green.svg)

A distributed Perforce integration service using Master-Agent architecture with event-driven state machine for reliable, scalable P4 operations.

## Features

- **Master-Agent Architecture** - Distributed execution with central control
- **Event-Driven State Machine** - Non-blocking, asynchronous job execution
- **Auto-Deploy** - Master automatically deploys Agents via SSH
- **Real-time Logs** - Live log streaming via TCP socket
- **Template System** - Save and reuse job configurations (global & private)
- **Scheduled Jobs** - Cron-based automated execution
- **Smart Conflict Resolution** - Auto-merge with manual fallback
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
2. Master deploys `agent_core.py` to remote machine via SSH
3. Agent connects back to Master on TCP port 9090
4. Master sends P4 commands, Agent executes and streams logs back

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

Server starts on `http://0.0.0.0:5000`.

To run in the background (persists after closing terminal):

```bash
nohup python wsgi.py > /tmp/p4_integ_server.log 2>&1 &
```

### 3. Access the Web UI

Open your browser and go to:

```
http://<server-hostname>:5000
```

For example: `http://atletx8-neu006:5000`

Log in with your P4 username and password. Credentials are validated against the P4 server.

## Sharing with Others

### If direct access works (no firewall)

Share the URL with your colleagues:

```
http://<server-hostname>:5000
```

They log in with their own P4 credentials.

### If blocked by firewall (most common)

Each user runs SSH port forwarding from their own machine:

```bash
ssh -L 5000:localhost:5000 <username>@<server-hostname>
```

For example:

```bash
ssh -L 5000:localhost:5000 zhangsan@atletx8-neu006
```

Then open `http://localhost:5000` in the browser. Keep the SSH session open while using the tool.

**Tip**: Add this to `~/.ssh/config` for convenience:

```
Host p4tool
    HostName atletx8-neu006
    User your_username
    LocalForward 5000 localhost:5000
```

Then just run `ssh p4tool` and open `http://localhost:5000`.

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
On cancel:                 -> kill process -> CLEANUP -> ERROR
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
| Dashboard | `/admin` | View all jobs with status |
| New Job | `/admin/submit` | Create integration job (with SSH config) |
| Job Detail | `/jobs/<id>` | View logs, resolve conflicts, cancel/retry |
| Templates | `/templates` | Save and reuse job configurations |
| Schedules | `/schedules` | Cron-based automated job execution |
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

### Templates
- `GET /api/templates` - List templates
- `POST /api/templates` - Create template
- `PUT /api/templates/<id>` - Update template
- `DELETE /api/templates/<id>` - Delete template
- `POST /api/templates/<id>/run` - Run job from template

### Schedules
- `GET /api/schedules` - List schedules
- `POST /api/schedules` - Create schedule
- `PUT /api/schedules/<id>` - Update schedule
- `DELETE /api/schedules/<id>` - Delete schedule
- `POST /api/schedules/<id>/run` - Run schedule immediately

### Agents
- `GET /api/agents` - List connected agents
- `GET /api/agents/status` - Agent connection status

## Configuration

Configuration is loaded from `config.yaml` (optional) and environment variables:

| Setting | Env Variable | Default | Description |
|---------|-------------|---------|-------------|
| P4 Port | `P4PORT` | - | Perforce server address |
| P4 User | `P4USER` | - | Perforce username |
| Log Level | `LOG_LEVEL` | INFO | Logging level |
| Data Dir | `P4_INTEG_DATA_DIR` | `./data` | Data storage directory |
| Flask Secret | `FLASK_SECRET_KEY` | dev key | Session encryption key |
| Server Host | `HOST` | 0.0.0.0 | Flask bind address |
| Server Port | `PORT` | 5000 | Flask port |

## Project Structure

```
p4-integration/
├── app/
│   ├── agent/
│   │   └── agent_core.py      # Remote agent (deployed via SSH)
│   ├── master/
│   │   ├── agent_server.py    # TCP server for agent connections
│   │   ├── job_state_machine.py  # Core state machine logic
│   │   ├── bootstrapper.py    # SSH agent deployment
│   │   └── workspace_queue.py # Workspace concurrency control
│   ├── models/
│   │   └── template.py        # Template management
│   ├── scheduler/
│   │   └── scheduler.py       # Cron-based scheduling
│   ├── templates/             # HTML templates (Jinja2 + Tailwind)
│   ├── api.py                 # REST API endpoints
│   ├── server.py              # Flask UI routes
│   ├── config.py              # Configuration loading
│   └── __init__.py            # App factory
├── data/
│   ├── templates/             # Stored templates (global/private)
│   └── logs/                  # Job logs
├── wsgi.py                    # Entry point
├── requirements.txt           # Python dependencies
└── README.md
```

## Troubleshooting

### Agent Connection Failed

1. Check that port 9090 is accessible from the remote machine:
   ```bash
   ssh user@remote "nc -zv <master-ip> 9090"
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

1. Verify the server is running on `0.0.0.0` (not `127.0.0.1`)
2. Test port connectivity: `nc -zv <hostname> 5000`
3. If blocked by firewall, use SSH port forwarding (see "Sharing with Others" above)

### P4 Login Issues

- The tool validates P4 credentials on login without affecting your existing P4 tickets
- If you get authentication errors, verify your password is correct: `p4 -p <P4PORT> -u <user> login -s`

## License

MIT
