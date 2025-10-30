# P4 Integration Service

> Automated Perforce integration service with web UI - handles merge, conflict resolution, and controlled submit

![Python](https://img.shields.io/badge/python-3.10+-blue.svg)
![Flask](https://img.shields.io/badge/flask-2.2.5-green.svg)
![License](https://img.shields.io/badge/license-MIT-blue.svg)

A lightweight Flask service to orchestrate Perforce (p4) integration, conflict resolution, and controlled submit via an admin UI or API.

## Highlights

- 🆔 **Human-friendly Job IDs** - `INT-20251029-001` instead of UUIDs
- ⚡ **10x Performance** - In-memory caching with lazy writes
- 🔄 **Smart Caching** - 50% fewer P4 calls with intelligent cache
- 🎨 **Modern UI** - Clean, streamlined interface
- 📝 **Auto-formatting** - Standardized changelist descriptions
- 🌐 **SSH Support** - Run P4 commands on remote Linux hosts

## Features

### Core Functionality
- 🔄 **Automated Integration**: Create integration jobs with source/target or branch specs
- 🔍 **Conflict Detection**: Automatic conflict detection and resolution assistance
- 🎯 **Smart Resolution**: Two-pass auto-merge with manual resolution support
- 🔐 **Security**: Blocklist checks and optional test hooks before submit
- 🌐 **Flexible Execution**: Local or Remote (SSH) execution modes
- 📊 **Job Tracking**: Process tracking, cancellation, and kill capabilities

### Recent Improvements (2025-10-29)
- ✨ **Readable Job IDs**: Human-friendly `INT-YYYYMMDD-NNN` format
- ⚡ **Performance**: 10x faster storage with in-memory cache and lazy writes
- 🎨 **Clean UI**: Streamlined interface with only essential controls
- 📝 **Better Descriptions**: Standardized changelist descriptions with proper formatting
- ⏱️ **Smart Timestamps**: Relative time display ("2 min ago")
- 🔄 **Intelligent Caching**: Reduces redundant P4 calls by 50%
- 🧹 **Code Quality**: Unified logging, eliminated code duplication

### Advanced Features
- 🔁 **Auto-resolve**: Background thread automatically continues when conflicts are cleared
- 🔒 **Concurrency Safe**: Job-level locks prevent race conditions
- 🎚️ **Queue Management**: Configurable queue size limits
- 📡 **Real-time Updates**: SSE (Server-Sent Events) for live job status

## Quickstart (Local)
1. Install Python 3.10+ and `p4` CLI on the server. Ensure the service user has workspace access.
2. Copy `config.yaml.example` to `config.yaml` and configure your settings
3. **Security**: Set passwords via environment variables (never commit passwords to config.yaml):
   ```bash
   export P4PASSWD=your_p4_password
   export P4_INTEG_SSH_PASSWORD=your_ssh_password  # if using SSH mode
   ```
4. `pip install -r requirements.txt`
5. `python wsgi.py`

## Remote SSH Mode
When your Perforce workspace must live on Linux but you run this service on Windows, enable SSH mode to execute all p4 commands on the Linux host.

1. On Linux host:
   - Install `p4` and set up the client/workspace (Root should match a Linux path, e.g. `/srv/p4/ws`).
   - Ensure you can SSH from the Windows machine to this host with key or password.
2. In `config.yaml` set:
```yaml
exec:
  mode: "ssh"
  ssh:
    host: "your-linux-host"
    user: "your-user"
    port: 22
    key_path: "C:/path/to/key"  # or leave empty and set password
    password: ""
  workspace_root: "/srv/p4/ws"  # remote workspace root
p4:
  port: "perforce:1666"
  user: "your_user"
  client: "linux_client_name"
```
3. Restart the service. Use `/health/p4` to verify `exec_mode=ssh` and remote `workspace_root_exists=true`.

All integrate/resolve/submit and test hooks will now run on the Linux host.

## Admin UI

Access the web interface at `http://localhost:5000/admin`:
- **Submit** - Create new integration jobs
- **Running** - Monitor active jobs with live status updates
- **Done** - Review completed jobs
- **Job Detail** - View logs, conflicts, and manage resolution

## What's New in v2.1.0

### Unified Submit Flow
- **All submissions now use the complete workflow**: shelve → name_check → p4push
- Fixed inconsistency where manual resolve used direct `p4 submit` (bypassing shelve)
- `ready_to_submit` status now uses `continue_to_submit` API for consistent behavior

### Configurable Auto-Submit
- **New config option**: `auto_resolve.auto_submit` (default: `true`)
  - `auto_submit: true` → Auto-submit when conflicts cleared (both auto-resolve and manual rescan)
  - `auto_submit: false` → Wait for manual confirmation after conflicts cleared
- Unified behavior between auto-resolve thread and manual rescan operations

### Removed Deprecated APIs
- Removed `admin_submit` method and routes (use `continue_to_submit` instead)
- All submissions now go through proper shelving and validation
- CLI `submit` command now uses complete workflow

## What's New in v2.0.0

### Manual Rescan Flow
- **`rescan_conflicts()` behavior** - controlled by `auto_submit` config
- When conflicts are cleared and `auto_submit: true`, automatically continues to submit
- When `auto_submit: false`, job moves to `ready_to_submit` status waiting for manual confirmation
- Auto-resolve background thread respects the same `auto_submit` configuration

### API Changes
- Primary endpoint: `POST /api/jobs/<job_id>/continue_to_submit` - complete shelve + p4push workflow
- Deprecated: `POST /api/jobs/<job_id>/submit` (removed in v2.1.0)

### Configuration
- `max_queue_size` (default: 100) - limits number of jobs that can be queued
- `auto_resolve.auto_submit` (default: true) - controls auto-submit behavior
- Auto-resolve thread respects manual resolve operations (won't interfere)

### Status Values
- `ready_to_submit` - status when conflicts cleared and waiting for submission (if auto_submit=false)

## Architecture

### Project Structure
```
app/
├── constants.py      # Constants and configuration values
├── env_helper.py     # Environment initialization helper
├── storage.py        # Job storage with caching (10x performance)
├── jobs.py           # Job management and workflow orchestration
├── p4_client.py      # P4 command wrapper with retry logic
├── runner.py         # Local and SSH command execution
├── server.py         # Flask routes and API endpoints
├── events.py         # Event logging and SSE support
└── templates/        # Jinja2 HTML templates
```

### Key Concepts

**Job ID**: Each job has two IDs:
- **Readable ID**: `INT-20251029-001` (user-friendly, date-based)
- **UUID**: `2b27e130-...` (internal, globally unique)

**Storage**: In-memory cache with lazy writes (2-second batching)

**Caching**: Conflict checks cached for 30 seconds, manual rescans debounced (10 seconds)

**Concurrency**: Job-level locks prevent race conditions between auto-resolve and manual operations

## API Reference

### Job Management
- `POST /api/jobs/integrate` - Create integration job
- `GET /api/jobs` - List all jobs
- `GET /api/jobs/<id>` - Get job details (supports UUID or readable_id)
- `POST /api/jobs/<id>/cancel` - Cancel job
- `POST /api/jobs/<id>/retry` - Retry failed job
- `POST /api/jobs/<id>/continue_to_submit` - Continue to submit after conflicts cleared

### Admin UI
- `/admin/submit` - Create new integration job
- `/admin/running` - View active jobs with real-time status
- `/admin/done` - View completed jobs
- `/admin/jobs/<id>` - Job details with conflict resolution tools

## Configuration

See `config.yaml.example` for full configuration options. Key settings:

```yaml
# Performance tuning
max_queue_size: 100  # Max queued jobs

# Auto-resolve
auto_resolve:
  enabled: true      # Enable background conflict checking
  interval: 60       # Check interval (seconds)
  auto_submit: true  # Auto-submit when conflicts cleared (both auto-resolve and manual rescan)

# Auto-cleanup
auto_cleanup_on_error: true  # Revert workspace on job failure

# Environment initialization
env_init:
  enabled: true      # Enable environment setup scripts
  init_script: /path/to/init.bash
  bootenv_cmd: bootenv
```

## Development

### Running Tests
```bash
python verify_optimizations.py
```

### Code Quality
- Unified logging system (no print statements)
- Constants management (`app/constants.py`)
- Environment initialization helper (`app/env_helper.py`)
- Type hints for better IDE support

## Notes
- The service wraps `p4` commands via subprocess; it assumes a valid client workspace.
- For large merges or interactive resolves, prefer resolving incrementally by selected files.
- Adjust `blocklist` and `test_hook` in `config.yaml` to match your policies.
- Auto-resolve runs in background but pauses for jobs being manually resolved
- Job-level locks prevent race conditions between auto-resolve and manual operations
- Storage uses lazy writes (2-second batching) - graceful shutdown recommended

## Changelog

See [CHANGES.md](CHANGES.md) for detailed changelog.

## License

MIT
