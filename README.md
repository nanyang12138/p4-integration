# P4 Integration Service

> Automated Perforce integration service with Master-Agent architecture for distributed execution

![Python](https://img.shields.io/badge/python-3.10+-blue.svg)
![Flask](https://img.shields.io/badge/flask-2.2.5-green.svg)
![License](https://img.shields.io/badge/license-MIT-blue.svg)

A distributed Perforce integration service using Master-Agent architecture with event-driven state machine for reliable, scalable P4 operations.

## 🚀 What's New: Master-Agent Architecture

### Key Improvements
- **Event-Driven State Machine**: Replaced SSH blocking with asynchronous event-based execution
- **Reverse Connection**: Agents connect to Master (firewall-friendly, no inbound ports needed)
- **Real-time Status**: Live status updates via TCP socket instead of PID polling
- **Auto-deployment**: Master automatically deploys Agent via SSH or local process
- **Cross-platform**: Master on Windows, Agents on Linux/Unix
- **Robust Error Handling**: Detailed connection failure diagnostics

## Highlights

- 🏗️ **Master-Agent Architecture** - Distributed execution with central control
- ⚡ **Event-Driven** - Non-blocking, asynchronous job execution
- 🔄 **Auto-Deploy** - Master automatically deploys and starts Agents
- 📡 **Real-time Updates** - Live status via TCP socket communication
- 🆔 **Human-friendly IDs** - `INT-20251029-001` format
- 🎯 **Smart Resolution** - Two-pass auto-merge with manual support
- 🎨 **Modern UI** - Clean, responsive interface with Tailwind CSS

## Architecture Overview

```
┌─────────────────┐          ┌─────────────────┐
│   Master (Web)  │          │   Agent (P4)    │
│                 │          │                 │
│  ┌───────────┐  │          │  ┌───────────┐  │
│  │   Flask   │  │          │  │  P4 CLI   │  │
│  │    UI     │  │          │  │ Executor  │  │
│  └─────┬─────┘  │          │  └─────▲─────┘  │
│        │        │          │        │        │
│  ┌─────▼─────┐  │  TCP     │  ┌────┴──────┐  │
│  │   State   │◄─┼──────────┼─►│   Agent   │  │
│  │  Machine  │  │  9090    │  │   Core    │  │
│  └───────────┘  │          │  └───────────┘  │
│                 │          │                 │
│  Windows/Linux  │          │  Linux/Unix     │
└─────────────────┘          └─────────────────┘
```

## Features

### Core Functionality
- 🔄 **Automated Integration**: Create jobs with source/target or branch specs
- 🎯 **Smart Conflict Resolution**: Two-pass auto-merge with manual support
- 📊 **Event-Driven State Machine**: SYNC → INTEGRATE → RESOLVE → SUBMIT → DONE
- 🌐 **Distributed Execution**: Run P4 commands on remote machines
- 🔐 **Security**: Blocklist checks and test hooks before submit

### Master-Agent Communication
- **Reverse Connection**: Agents initiate connection to Master
- **JSON Protocol**: Line-based JSON messages
- **Command Types**: EXEC_CMD, KILL_CMD, SHUTDOWN
- **Event Types**: LOG, CMD_DONE, ERROR, HEARTBEAT
- **Auto-reconnect**: Agents retry connection on network failures

## Quick Start

### 1. Configure Master (`config.yaml`)

```yaml
# P4 Configuration
p4:
  port: perforce:1666
  user: your_user
  client: your_client
  password: ''  # Use environment variable P4PASSWD
  bin: /path/to/p4

# Agent Configuration (Master's listening address)
agent:
  master_host: 10.67.190.37  # Your Master's IP
  master_port: 9090

# SSH Configuration (for Agent deployment)
ssh:
  host: srdcws990  # or 'localhost' for local mode
  user: your_user
  port: 22
  key_path: ~/.ssh/id_rsa
  password: ''  # Optional fallback

# Workspace
workspace:
  root: /path/to/workspace
```

### 2. Start Master

```bash
# Install dependencies
pip install -r requirements.txt

# Start Master (Windows or Linux)
python app.py
```

The Master will:
1. Start Flask web UI on port 5000
2. Start Agent TCP server on port 9090
3. Wait for Agent connections

### 3. Deploy Agent

When you create a job via the web UI:
1. Master automatically deploys Agent to the configured SSH host
2. Agent connects back to Master on TCP port 9090
3. Master sends commands, Agent executes and streams logs

## Job Workflow

### State Machine Flow

```
INIT → SYNC → INTEGRATE → RESOLVE_PASS_1 → RESOLVE_PASS_2 
  ↓                              ↓              ↓
ERROR                   RESOLVE_CHECK ←─────────┘
  ↑                              ↓
  └──────────────────── NEEDS_RESOLVE (manual)
                                 ↓
                         PRE_SUBMIT → SHELVE → P4PUSH → DONE
                                        ↓
                                    NC_FIX (if needed)
```

### Stage Details

1. **SYNC**: Update workspace to latest
2. **INTEGRATE**: Perform P4 integration
3. **RESOLVE_PASS_1**: First auto-resolve attempt
4. **RESOLVE_PASS_2**: Second auto-resolve attempt
5. **RESOLVE_CHECK**: Check for remaining conflicts
6. **NEEDS_RESOLVE**: Wait for manual resolution
7. **PRE_SUBMIT**: Run test hooks and blocklist checks
8. **SHELVE**: Create shelved changelist
9. **NC_FIX**: Fix naming conflicts if any
10. **P4PUSH**: Push to target
11. **DONE**: Job completed successfully

## Web Interface

Access at `http://localhost:5000`:

- **Dashboard** (`/admin`): View all jobs with status
- **New Job** (`/admin/submit`): Create integration jobs
- **Job Detail** (`/jobs/<id>`): View logs, resolve conflicts

### Status Indicators
- 🟢 **Connected**: Agent online and ready
- 🟡 **Connecting**: Waiting for Agent connection
- ⚫ **Offline**: No Agent connected

## API Reference

### Job Management
- `POST /api/jobs` - Create new job
- `GET /api/jobs` - List all jobs
- `GET /api/jobs/<id>` - Get job details
- `GET /api/jobs/<id>/logs` - Get job logs
- `POST /api/jobs/<id>/continue` - Continue after manual resolve

### Agent Status
- `GET /api/agents` - List connected agents
- `GET /api/agents/status` - Get agent connection status

## Troubleshooting

### Agent Connection Failed

If you see "Agent Connection Failed" error:

1. **Check Firewall**: Ensure port 9090 is open on Master
   ```bash
   # Windows
   netsh advfirewall firewall add rule name="P4 Master" dir=in action=allow protocol=TCP localport=9090
   
   # Linux
   sudo firewall-cmd --add-port=9090/tcp --permanent
   ```

2. **Test Connectivity**: From Agent machine
   ```bash
   telnet <master_ip> 9090
   # or
   nc -zv <master_ip> 9090
   ```

3. **Check Agent Process**:
   ```bash
   ps aux | grep agent_core
   ```

### SSH Authentication Failed

1. **Test SSH manually**:
   ```bash
   ssh user@host "echo OK"
   ```

2. **Use password authentication**: Add to `config.yaml`
   ```yaml
   ssh:
     password: 'your_password'
   ```

3. **Or use local mode**:
   ```yaml
   ssh:
     host: localhost
   ```

## Architecture Details

### Master Components

- **Flask App**: Web UI and REST API
- **Agent Server**: TCP server for Agent connections
- **Job State Machine**: Event-driven job orchestration
- **Bootstrapper**: Agent deployment via SSH

### Agent Components

- **Agent Core**: TCP client, command executor
- **Command Executor**: Subprocess management
- **Log Streamer**: Real-time log forwarding

### Communication Protocol

```json
// Agent → Master
{"type": "REGISTER", "hostname": "agent01", "workspace": "/p4/ws"}
{"type": "LOG", "data": "p4 sync output...", "stream": "stdout"}
{"type": "CMD_DONE", "exit_code": 0}
{"type": "ERROR", "message": "Command failed"}

// Master → Agent  
{"type": "EXEC_CMD", "cmd_id": "123", "command": "p4 sync ..."}
{"type": "KILL_CMD", "cmd_id": "123"}
{"type": "SHUTDOWN"}
```

## Development

### Project Structure
```
p4-integration/
├── app/
│   ├── agent/              # Agent module
│   │   └── agent_core.py   # Main agent logic
│   ├── master/             # Master module
│   │   ├── agent_server.py # TCP server
│   │   ├── job_state_machine.py # State machine
│   │   └── bootstrapper.py # Agent deployment
│   ├── templates/          # Web UI templates
│   ├── api.py             # REST API
│   └── server.py          # Flask routes
├── config.yaml            # Configuration
└── app.py                # Entry point
```

### Adding New Stages

1. Add to `Stage` enum in `job_state_machine.py`
2. Define command in `_get_stage_command()`
3. Add transition logic in `_decide_next_stage()`

## Notes

- Master can run on Windows or Linux
- Agents typically run on Linux/Unix (where P4 workspaces exist)
- One Master can manage multiple Agents
- Agents auto-reconnect on network failures
- All commands run with proper environment setup

## License

MIT