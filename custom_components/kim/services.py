"""Service schemas for KIM.

The actual handlers are registered per config entry in ``runtime.py`` (they
need the live manager instance). This module only defines the validation
schemas so the Home Assistant UI can render the services correctly.
"""
from __future__ import annotations

import voluptuous as vol

from homeassistant.helpers import config_validation as cv

from .const import (
    SERVICE_ARCHIVE,
    SERVICE_ARCHIVE_LIST,
    SERVICE_ARCHIVE_LOAD,
    SERVICE_CANCEL,
    SERVICE_COLLECT_ARTIFACTS,
    SERVICE_FETCH_FILE,
    SERVICE_NOTE,
    SERVICE_PAUSE,
    SERVICE_RESUME,
    SERVICE_RETRY,
    SERVICE_SUBMIT,
)

SUBMIT_SCHEMA = vol.Schema(
    {
        vol.Required("goal"): cv.string,
        vol.Optional("title"): cv.string,
        vol.Optional("plan"): cv.string,
    }
)

TASK_SCHEMA = vol.Schema({vol.Required("task_id"): cv.string})

NOTE_SCHEMA = vol.Schema(
    {
        vol.Required("text"): cv.string,
        vol.Optional("author", default="user"): cv.string,
        vol.Optional("task_id"): cv.string,
    }
)

FETCH_FILE_SCHEMA = vol.Schema(
    {
        vol.Required("task_id"): cv.string,
        vol.Required("path"): cv.string,
        vol.Optional("save_as"): cv.string,
    }
)

COLLECT_SCHEMA = vol.Schema({vol.Required("task_id"): cv.string})

ARCHIVE_LOAD_SCHEMA = vol.Schema({vol.Required("name"): cv.string})

EMPTY_SCHEMA = vol.Schema({})

SCHEMAS: dict[str, vol.Schema] = {
    SERVICE_SUBMIT: SUBMIT_SCHEMA,
    SERVICE_CANCEL: TASK_SCHEMA,
    SERVICE_RETRY: TASK_SCHEMA,
    SERVICE_PAUSE: EMPTY_SCHEMA,
    SERVICE_RESUME: EMPTY_SCHEMA,
    SERVICE_NOTE: NOTE_SCHEMA,
    SERVICE_FETCH_FILE: FETCH_FILE_SCHEMA,
    SERVICE_COLLECT_ARTIFACTS: COLLECT_SCHEMA,
    SERVICE_ARCHIVE: EMPTY_SCHEMA,
    SERVICE_ARCHIVE_LIST: EMPTY_SCHEMA,
    SERVICE_ARCHIVE_LOAD: ARCHIVE_LOAD_SCHEMA,
}
