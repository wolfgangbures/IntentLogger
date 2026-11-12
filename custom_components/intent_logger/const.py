"""Constants for Intent Logger."""

DOMAIN = "intent_logger"
EVENT_FAILED = "intent_logger_failed"
CONF_PIPELINE_IDS = "pipeline_ids"
CONF_RETENTION_DAYS = "retention_days"
CONF_MAX_FILE_MB = "max_file_mb"
CONF_MAX_FILES = "max_files"
CONF_FAILURE_EVENTS = "failure_events"
CONF_IGNORE_DUPLICATE_WAKE_UPS = "ignore_duplicate_wake_ups"
IGNORED_PIPELINE_ERROR_CODES = frozenset({"duplicate_wake_up_detected"})
DEFAULTS = {
    CONF_PIPELINE_IDS: [],
    CONF_RETENTION_DAYS: 30,
    CONF_MAX_FILE_MB: 10,
    CONF_MAX_FILES: 20,
    CONF_FAILURE_EVENTS: True,
    CONF_IGNORE_DUPLICATE_WAKE_UPS: True,
}
