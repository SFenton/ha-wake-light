"""Constants for the Wake Light integration."""

from __future__ import annotations

DOMAIN = "wake_light"
PLATFORMS = ["sensor"]

SERVICE_COMMAND = "command"

CONF_PROFILE_ID = "profile_id"
CONF_PROFILE_NAME = "profile_name"
CONF_AREA_ID = "area_id"
CONF_ROOT_LIGHT_ENTITY_ID = "root_light_entity_id"
CONF_TARGET_LIGHT_ENTITY_IDS = "target_light_entity_ids"
CONF_OCCUPANCY_ENTITY_ID = "occupancy_entity_id"
CONF_PBL_SWITCH_ENTITY_ID = "pbl_switch_entity_id"
CONF_VACATION_ENTITY_ID = "vacation_entity_id"
CONF_BLOCKER_ENTITY_IDS = "blocker_entity_ids"
CONF_SLEEPYPOD_SCHEDULE_ENTITY_ID = "sleepypod_schedule_entity_id"
CONF_SLEEPYPOD_SCHEDULE_SET_TOPIC = "sleepypod_schedule_set_topic"
CONF_SLEEPYPOD_SOURCE_SIDES = "sleepypod_source_sides"
CONF_SLEEPYPOD_LEFT_STATE_ENTITY_ID = "sleepypod_left_state_entity_id"
CONF_SLEEPYPOD_RIGHT_STATE_ENTITY_ID = "sleepypod_right_state_entity_id"
CONF_DEFAULT_RAMP_MINUTES = "default_ramp_minutes"
CONF_DEFAULT_POST_WAKE_HOLD_MINUTES = "default_post_wake_hold_minutes"
CONF_LEGACY_BRIGHTNESS_LIFECYCLE_SAFE = "legacy_brightness_lifecycle_safe"

DEFAULT_RAMP_MINUTES = 30
DEFAULT_POST_WAKE_HOLD_MINUTES = 5
DEFAULT_MISSED_ALARM_CATCHUP_SECONDS = 10 * 60
DEFAULT_RAMP_STEP_SECONDS = 30
DEFAULT_SLEEPYPOD_SCHEDULE_SET_TOPIC = "sleepypod/eight-pod/cmd/set-schedules"
TEMPORARY_BED_ALARM_CLEANUP_GRACE_SECONDS = 10 * 60
TEMPORARY_BED_ALARM_RETRY_SECONDS = 60
GROUP_OFF_SETTLE_SECONDS = 0.25
MIN_BRIGHTNESS_SETTLE_SECONDS = 5

RAMP_MINUTE_OPTIONS = (0, 5, 10, 15, 30)
MIN_RAMP_MINUTES = 0
MAX_RAMP_MINUTES = 60
# Version 1 intentionally has one fixed completion hold. The field remains in
# the profile/read model so a later version can migrate it without a new API.
MIN_POST_WAKE_HOLD_MINUTES = 5
MAX_POST_WAKE_HOLD_MINUTES = 5

MAX_SNOOZE_BUDGET_SECONDS = 30 * 60
RECOVERY_BUDGET_SECONDS = 10 * 60
RECOVERY_RETRY_BUDGET_SECONDS = 90
MAX_PBL_TTL_SECONDS = 7200
MAX_EPISODE_SECONDS = (
    MAX_RAMP_MINUTES * 60
    + DEFAULT_POST_WAKE_HOLD_MINUTES * 60
    + MAX_SNOOZE_BUDGET_SECONDS
    + RECOVERY_BUDGET_SECONDS
)
MAX_ACTIVE_OCCURRENCES = 8
MAX_TARGET_LIGHTS = 32
MAX_REQUEST_HISTORY = 64
MAX_TERMINAL_OCCURRENCES = 256
MAX_CANCELLATION_OCCURRENCE_REFS = 16
MAX_FAILURES = 16
MAX_NATIVE_ALARMS = 64
MAX_SOURCE_RECORDS_PER_SIDE = 32
MAX_ALARM_LINKS = 512

STORE_VERSION = 1
STORE_MINOR_VERSION = 5
STORE_KEY_PREFIX = f"{DOMAIN}.profile"

ALARM_KIND_ONCE = "once"
ALARM_KIND_WEEKLY = "weekly"
ALARM_KINDS = {ALARM_KIND_ONCE, ALARM_KIND_WEEKLY}
ALARM_SOURCE_NATIVE = "native"
ALARM_SOURCE_SLEEPYPOD = "sleepypod"
ALARM_SOURCES = {ALARM_SOURCE_NATIVE, ALARM_SOURCE_SLEEPYPOD}

WEEKDAYS = (
    "sunday",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
)
WEEKDAY_TO_PYTHON = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

PHASE_IDLE = "idle"
PHASE_SCHEDULED = "scheduled"
PHASE_BLOCKED_VACATION = "blocked_vacation"
PHASE_RAMPING = "ramping"
PHASE_SNOOZED = "snoozed"
PHASE_HOLDING = "holding"
PHASE_DEGRADED = "degraded"
PHASE_UNAVAILABLE = "unavailable"
PHASES = {
    PHASE_IDLE,
    PHASE_SCHEDULED,
    PHASE_BLOCKED_VACATION,
    PHASE_RAMPING,
    PHASE_SNOOZED,
    PHASE_HOLDING,
    PHASE_DEGRADED,
    PHASE_UNAVAILABLE,
}

OP_UPSERT_ALARM = "upsert_alarm"
OP_DELETE_ALARM = "delete_alarm"
OP_UPDATE_DEFAULTS = "update_defaults"
OP_LINK_ALARM = "link_alarm"
OP_DISMISS = "dismiss"
OP_CANCEL_OCCURRENCE = "cancel_occurrence"
OP_END_EPISODE = "end_episode"
COMMAND_OPERATIONS = {
    OP_UPSERT_ALARM,
    OP_DELETE_ALARM,
    OP_UPDATE_DEFAULTS,
    OP_LINK_ALARM,
    OP_DISMISS,
    OP_CANCEL_OCCURRENCE,
    OP_END_EPISODE,
}

SOURCE_SIDE_LEFT = "left"
SOURCE_SIDE_RIGHT = "right"
SOURCE_SIDES = (SOURCE_SIDE_LEFT, SOURCE_SIDE_RIGHT)
SOURCE_REF_PREFIX = "sleepypod:"

PBL_DOMAIN = "presence_based_lighting"
PBL_SERVICE_ACQUIRE = "acquire_control"
PBL_SERVICE_DISPATCH = "dispatch_control"
PBL_SERVICE_RELEASE = "release_control"
PBL_EVENT_LEASE_REVOKED = "presence_based_lighting_control_lease_revoked"
PBL_OWNER = "wake_light"
PBL_ACQUIRE_SUCCESS_OUTCOMES = {"acquired", "duplicate", "recovered", "updated"}
PBL_DISPATCH_SUCCESS_OUTCOMES = {"dispatched"}
PBL_RELEASE_SUCCESS_OUTCOMES = {"already_terminal", "released"}

PBL_RELEASE_OUTCOME_COMPLETED = "completed"
PBL_RELEASE_OUTCOME_CANCELLED = "cancelled"
PBL_RELEASE_OUTCOME_FAILED = "failed"
PBL_RELEASE_CAUSE_HOLD_COMPLETE = "hold_complete"
PBL_RELEASE_CAUSE_OCCURRENCE_CANCELLED = "occurrence_cancelled"
PBL_RELEASE_CAUSE_EXTERNAL_OFF = "external_targets_off"
PBL_RELEASE_CAUSE_OWNER_FAILED = "owner_failed"
PBL_RELEASE_CAUSE_OWNER_SHUTDOWN = "owner_shutdown"

EVENT_WAKE_LIGHT_OUTCOME = "wake_light_outcome"

STATE_UNKNOWN = "unknown"
STATE_UNAVAILABLE = "unavailable"
UNAVAILABLE_STATES = {STATE_UNKNOWN, STATE_UNAVAILABLE}

OUTCOME_READY = "ready"
OUTCOME_ACCEPTED = "accepted"
OUTCOME_NO_CHANGE = "no_change"
OUTCOME_REVISION_CONFLICT = "revision_conflict"
OUTCOME_REQUEST_ID_CONFLICT = "request_id_conflict"
OUTCOME_INVALID_REQUEST = "invalid_request"
OUTCOME_NOT_FOUND = "not_found"
OUTCOME_READ_ONLY_SOURCE = "read_only_source"
OUTCOME_NO_ACTIVE_OCCURRENCE = "no_active_occurrence"
OUTCOME_SNOOZE_BUDGET_EXHAUSTED = "snooze_budget_exhausted"
OUTCOME_CANCELLED_BY_USER = "cancelled_by_user"

USER_CANCELLATION_CAUSES = {
    "bulk_off",
    "controlled_entity_off",
    "manual_brightness_control",
    "manual_group_off",
}

FAILURE_LEGACY_LIFECYCLE_UNRESOLVED = (
    "legacy_brightness_255_lifecycle_unresolved"
)
FAILURE_INTEGRATION_UNAVAILABLE = "integration_unavailable"
FAILURE_VACATION_BLOCKED = "vacation_not_off"
FAILURE_PBL_NOT_READY = "pbl_not_ready"
FAILURE_TARGET_UNAVAILABLE = "target_unavailable"
FAILURE_BLOCKER_NOT_OFF = "blocker_not_off"
FAILURE_INVALID_SCHEDULE = "invalid_schedule"
FAILURE_PBL_ACQUIRE = "pbl_acquire_failed"
FAILURE_PBL_DISPATCH = "pbl_dispatch_failed"
FAILURE_PBL_RELEASE = "pbl_release_failed"
FAILURE_MANUAL_REVOKE = "manual_group_off"
FAILURE_NO_TARGETS = "no_owned_targets"
FAILURE_MISSED = "missed_alarm"
FAILURE_SOURCE_UNAVAILABLE = "source_unavailable"
FAILURE_RECOVERY_EXPIRED = "recovery_catchup_expired"
FAILURE_OCCURRENCE_LIMIT = "occurrence_limit_reached"
