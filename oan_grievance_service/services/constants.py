"""Canonical vocabulary from the FSD. Every module imports status names from here.

Hardcoding a status string in more than one place is how a lifecycle drifts, so
FSD 3.4's canonical list lives here once.
"""

# FSD 3.4 canonical lifecycle. "More Info Needed" is absent from the 3.4 table but
# required by 3.5, Appendix C and Appendix D-2, so it is part of the canonical set.
SUBMITTED = "Submitted"
ASSIGNED = "Assigned"
IN_PROGRESS = "In Progress"
MORE_INFO_NEEDED = "More Info Needed"
PENDING_SUBMITTER = "Pending Submitter"
RESOLVED = "Resolved"
CLOSED = "Closed"
REJECTED = "Rejected"

OPEN_STATUSES = (
	SUBMITTED,
	ASSIGNED,
	IN_PROGRESS,
	MORE_INFO_NEEDED,
	PENDING_SUBMITTER,
)

TERMINAL_STATUSES = (CLOSED, REJECTED)

# FSD 3.4: the only legal moves. Anything not listed here is refused.
ALLOWED_TRANSITIONS = {
	SUBMITTED: {ASSIGNED, REJECTED},
	ASSIGNED: {IN_PROGRESS, REJECTED},
	IN_PROGRESS: {MORE_INFO_NEEDED, PENDING_SUBMITTER, ASSIGNED, REJECTED},
	MORE_INFO_NEEDED: {IN_PROGRESS, REJECTED},
	PENDING_SUBMITTER: {RESOLVED, IN_PROGRESS, CLOSED},
	RESOLVED: {CLOSED, IN_PROGRESS},
	CLOSED: {IN_PROGRESS},  # FSD 3.6 reopen
	REJECTED: set(),
}

# FSD 3.4: transitions the submitter must justify.
REASON_REQUIRED_TO = {REJECTED}
# FSD 3.6 / 4.2 step 6b: a reopen always carries a mandatory reason.
REOPEN_TARGET = IN_PROGRESS

# FSD 3.11.3: display groups mapped to canonical statuses. The mapping is required
# to be configurable and documented; this is the documented default.
DISPLAY_GROUPS = {
	"All": None,
	"Pending": (SUBMITTED, ASSIGNED),
	"In Progress": (IN_PROGRESS, MORE_INFO_NEEDED),
	"Under Review": (PENDING_SUBMITTER,),
	"Resolved": (RESOLVED, CLOSED),
	"Rejected": (REJECTED,),
}

# FSD Appendix D-2: the response outcome drives the next status.
RESPONSE_OUTCOME_NEXT_STATUS = {
	"Resolved": PENDING_SUBMITTER,
	"Partially Resolved": PENDING_SUBMITTER,
	"Referred to another dept": ASSIGNED,
	"Requires further info": MORE_INFO_NEEDED,
}

# FSD Appendix C event codes. Each is the "method" on one core Notification record per
# channel, seeded by setup/install.py and editable from the desk thereafter.
EVENT_SUBMISSION_RECEIVED = "submission_received"
EVENT_DUPLICATE_DETECTED = "duplicate_detected"
EVENT_ASSIGNED_AUTO = "grievance_assigned_auto"
EVENT_ASSIGNED_MANUAL = "grievance_assigned_manual"
EVENT_STATUS_IN_PROGRESS = "status_in_progress"
EVENT_MORE_INFO_REQUESTED = "more_info_requested"
EVENT_SUBMITTER_RESPONDED = "submitter_responds_to_info"
EVENT_RESPONSE_SENT = "structured_response_sent"
EVENT_CONFIRMATION_WINDOW = "confirmation_window_open"
EVENT_CONFIRMED = "grievance_confirmed"
EVENT_REOPENED = "grievance_reopened"
EVENT_AUTO_CLOSED = "auto_closed_no_response"
EVENT_CLOSED = "grievance_closed"
EVENT_SLA_REMINDER_50 = "sla_reminder_50"
EVENT_SLA_REMINDER_80 = "sla_reminder_80"
EVENT_SLA_AT_RISK = "sla_at_risk_report"
EVENT_SLA_BREACH_L1 = "sla_breached_l1"
EVENT_SLA_BREACH_L2 = "sla_breached_l2"
EVENT_MANUAL_ESCALATION = "manual_escalation"
EVENT_REASSIGNMENT_REQUESTED = "reassignment_requested"

# FSD 3.6: default submitter confirmation window.
DEFAULT_CONFIRMATION_DAYS = 7
# FSD 3.7: reminder thresholds as a percentage of the SLA window.
SLA_REMINDER_THRESHOLDS = (50, 80)
# FSD 3.11.7: global default ceiling on a single deferral.
DEFAULT_MAX_DEFERRAL_DAYS = 30
