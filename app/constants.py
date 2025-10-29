"""
Constants and enums for P4 Integration Service
"""
from enum import Enum


# Job statuses
class JobStatus:
    QUEUED = "queued"
    PENDING = "pending"
    RUNNING = "running"
    NEEDS_RESOLVE = "needs_resolve"
    READY_TO_SUBMIT = "ready_to_submit"
    BLOCKED = "blocked"
    PUSHED = "pushed"
    PUSHED_TRIAL = "pushed_trial"
    ERROR = "error"
    CANCELLED = "cancelled"
    
    # Status sets
    TERMINAL_STATUSES = {PUSHED, PUSHED_TRIAL, ERROR, CANCELLED, BLOCKED}
    ACTIVE_STATUSES = {QUEUED, RUNNING, PENDING, NEEDS_RESOLVE, READY_TO_SUBMIT}


# Job stages
class JobStage:
    PENDING = "pending"
    SYNC = "sync"
    INTEGRATE = "integrate"
    RESOLVING = "resolving"
    RESOLVE = "resolve"
    PRE_SUBMIT_CHECKS = "pre_submit_checks"
    SHELVE = "shelve"
    RESHELVE = "reshelve"
    P4PUSH = "p4push"
    SUBMIT = "submit"
    PUSHED = "pushed"
    BLOCKED = "blocked"
    ERROR = "error"


# Default configuration values
class Defaults:
    # Paths
    DEFAULT_WORKSPACE_ROOT = "ws"
    DEFAULT_DATA_DIR = "data"
    DEFAULT_P4_BIN = "p4"
    DEFAULT_SHELL = "/bin/bash"
    
    # Environment initialization
    DEFAULT_INIT_SCRIPT = "/proj/verif_release_ro/cbwa_initscript/current/cbwa_init.bash"
    DEFAULT_BOOTENV_CMD = "bootenv"
    DEFAULT_NAME_CHECK_TOOL = "/tool/aticad/1.0/src/perforce/name_check_file_list"
    
    # P4 merge tool
    DEFAULT_MERGE_BIN = "/tool/pandora64/bin/p4merge"
    
    # Queue and concurrency
    DEFAULT_MAX_QUEUE_SIZE = 100
    DEFAULT_BATCH_SIZE = 50  # For reopen/revert operations
    
    # Retry configuration
    DEFAULT_MAX_RETRIES = 2
    DEFAULT_RETRY_BACKOFF_MS = 300
    
    # Auto-resolve
    DEFAULT_AUTO_RESOLVE_INTERVAL = 60  # seconds
    
    # Cache
    DEFAULT_CONFLICT_CACHE_TTL = 30  # seconds
    DEFAULT_STORAGE_WRITE_INTERVAL = 2.0  # seconds
    
    # Debounce
    DEFAULT_RESCAN_DEBOUNCE = 10  # seconds


# Job ID formats
class JobIdFormat:
    PREFIX = "INT"
    DATE_FORMAT = "%Y%m%d"
    SEQUENCE_DIGITS = 3
    
    @classmethod
    def generate_readable_id(cls, date_str: str, sequence: int) -> str:
        """Generate readable job ID: INT-YYYYMMDD-NNN"""
        return f"{cls.PREFIX}-{date_str}-{sequence:0{cls.SEQUENCE_DIGITS}d}"


# P4 command timeouts and limits
class P4Limits:
    MAX_NAME_CHECK_PASSES = 5
    
    
# File artifacts
class ArtifactNames:
    PRE_LOG = "pre.log"
    INTEGRATE_LOG = "integrate_out.log"
    RESOLVE_PASS_PREFIX = "resolve_pass"
    RESOLVE_CURRENT = "resolve_current.log"
    RESOLVE_PREVIEW = "resolve_preview.log"
    CONFLICTS = "conflicts.log"
    OPENED = "opened.log"
    SHELVE_OUT = "shelve_out.log"
    P4PUSH_OUT = "p4push_out.log"
    NAME_CHECK = "name_check.log"
    NAME_CHECK_FILE_LIST = "name_check_file_list"
    REVERT = "revert.log"
    STATUS_JSON = "status.json"
    EVENTS_JSONL = "events.jsonl"

