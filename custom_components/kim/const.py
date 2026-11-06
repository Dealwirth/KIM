"""Constants for the KIM (KI Manager) integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "kim"

# Configuration keys
CONF_BACKEND = "backend"
CONF_BASE_URL = "base_url"
CONF_API_KEY = "api_key"
CONF_MODEL = "model"
CONF_REPOSITORY = "repository"
CONF_BRANCH = "branch"
CONF_MAX_PARALLEL = "max_parallel"
CONF_HEARTBEAT_TIMEOUT = "heartbeat_timeout"
CONF_CLAIM_TTL = "claim_ttl"
CONF_STALL_TIMEOUT = "stall_timeout"
CONF_WORKSPACE = "workspace"
CONF_MAX_RETRIES = "max_retries"
CONF_ROLES = "roles"
CONF_IDLE_WORKER_TTL = "idle_worker_ttl"
CONF_LOCAL_URL = "local_url"
CONF_LOCAL_API_KEY = "local_api_key"
CONF_ARCHIVE_REPO = "archive_repo"
CONF_RETRY_BACKOFF = "retry_backoff"

# Backend types
BACKEND_CLOUD = "cloud"
BACKEND_LOCAL = "local"
BACKEND_MOCK = "mock"
# Automatic, free-first chain: Cloud -> Local -> Mock with quota cooldown.
BACKEND_AUTO = "auto"

BACKENDS = [BACKEND_AUTO, BACKEND_CLOUD, BACKEND_LOCAL, BACKEND_MOCK]

# Defaults
DEFAULT_BASE_URL = "https://app.all-hands.dev"
DEFAULT_MODEL = "openhands/deepseek-v4.1-flash"

# Models known to work on the OpenHands Cloud proxy for a standard account.
# Offered as a dropdown so a locked model cannot be configured by accident.
KNOWN_MODELS = [
    "openhands/deepseek-v4.1-flash",
    "openhands/deepseek-v4-flash",
    "openhands/deepseek-v4-pro",
    "openhands/deepseek-chat",
    "openhands/kimi-k3",
    "openhands/kimi-k2.7-code",
    "openhands/minimax-m3",
    "openhands/glm-5.2",
    "openhands/qwen3-coder-next",
    "openhands/gpt-5.6",
    "openhands/claude-sonnet-5",
    "openhands/gemini-3.8-flash",
]

DEFAULT_MAX_PARALLEL = 3
DEFAULT_HEARTBEAT_TIMEOUT = 180
DEFAULT_CLAIM_TTL = 900
DEFAULT_STALL_TIMEOUT = 900
DEFAULT_WORKSPACE = "/workspace"
# Agents are told to write their deliverables here; KIM archives this folder
# before a sandbox is torn down so PDFs/reports survive.
OUTPUT_DIR = "/workspace/project/output"
DEFAULT_MAX_RETRIES = 1
# Base delay (seconds) before a failed task is retried; doubles each attempt up
# to a cap. 0 disables the wait (used by the test suite).
DEFAULT_RETRY_BACKOFF = 5.0
DEFAULT_RETRY_BACKOFF_MAX = 120.0
# Agents are started on demand and retired after this many idle seconds so a
# quiet KIM does not keep paid sandboxes alive.
DEFAULT_IDLE_WORKER_TTL = 900

# Coordinator polling
UPDATE_INTERVAL = timedelta(seconds=15)
HEARTBEAT_INTERVAL = 30

# Worker lifecycle states
STATE_IDLE = "idle"
STATE_WORKING = "working"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_STALLED = "stalled"
STATE_OFFLINE = "offline"

# Task lifecycle states
TASK_PENDING = "pending"
TASK_ASSIGNED = "assigned"
TASK_IN_PROGRESS = "in_progress"
TASK_BLOCKED = "blocked"
TASK_DONE = "done"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"

TERMINAL_TASK_STATES = {TASK_DONE, TASK_FAILED, TASK_CANCELLED}

# Service names
SERVICE_SUBMIT = "submit"
SERVICE_CANCEL = "cancel"
SERVICE_PAUSE = "pause"
SERVICE_RESUME = "resume"
SERVICE_RETRY = "retry"
SERVICE_NOTE = "note"
SERVICE_FETCH_FILE = "fetch_file"
SERVICE_COLLECT_ARTIFACTS = "collect_artifacts"
SERVICE_ARCHIVE = "archive"
SERVICE_ARCHIVE_LIST = "archive_list"
SERVICE_ARCHIVE_LOAD = "archive_load"

# Where collected agent artifacts are published (HA serves /local/ from www/)
ARTIFACT_DIR = "www/kim"

# Scratchpad
NOTES_FILE = "notes.jsonl"

# Assignment policies
POLICY_AFFINITY = "affinity"
POLICY_LEAST_LOADED = "least_loaded"
POLICY_ROUND_ROBIN = "round_robin"

# Events fired on the HA bus
EVENT_TASK_DONE = f"{DOMAIN}_task_done"
EVENT_TASK_FAILED = f"{DOMAIN}_task_failed"
EVENT_TASK_BLOCKED = f"{DOMAIN}_task_blocked"
EVENT_WORKER_OFFLINE = f"{DOMAIN}_worker_offline"
