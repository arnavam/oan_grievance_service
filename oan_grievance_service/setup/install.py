"""Seed data created on install and refreshed on migrate.

Everything here is configuration the FSD names explicitly, so a fresh site comes up
usable rather than empty: the three capability roles, the five service categories from
3.2.2, the Ethiopian regions from 3.11.8, and the full Appendix C notification matrix.
"""

import gzip
import os

import frappe

from oan_grievance_service.services import constants as C

# The three capability roles. Appendix F's L1 / L2 / Department Head were rungs of a
# hierarchy rather than distinct capabilities, and are replaced by position in the
# reporting chain - see .docs/sla_workflows_and_lifecycle_specification.md §10.1.
#
# No desk access for any role for now. Every actor - submitter, officer and admin -
# reaches the system through the portal and the v1 API, so Desk is not part of the
# surface being built or secured. Turning it on later is a one-line change per role,
# but it widens the attack surface to every doctype the role holds DocPerm on, so it
# should be a deliberate decision rather than a default.
ROLES = [
	("Grievance Submitter", 0),
	("Grievance Officer", 0),
	("Grievance Admin", 0),
]

# FSD 3.2.2. The code is the CATEGORY segment of the FSD 3.2.3 ticket number.
SERVICE_CATEGORIES = [
	("Inputs", "INPT", 1),
	("Schemes", "SCHM", 2),
	("Payments", "PAYM", 3),
	("Credit", "CRDT", 4),
	("Markets", "MRKT", 5),
]

# Submitter Types master
SUBMITTER_TYPES = [
	("Individual Farmer", "IND"),
	("Development Agent", "DA"),
	("Cooperative", "COOP"),
	("FPO", "FPO"),
	("NGO", "NGO"),
	("Woreda/Kebele Body", "BODY"),
]

# Submission Types / Channels master
SUBMISSION_TYPES = [
	("Mobile App", "APP"),
	("Web Portal", "WEB"),
	("Mobile Call", "CALL"),
	("IVR Helpline", "IVR"),
	("Development Agent Assisted", "DA"),
]

# FSD Appendix C, the complete notification matrix.
# (event_code, title, recipient, channel, trigger, message template)
NOTIFICATION_EVENTS = [
	(
		C.EVENT_SUBMISSION_RECEIVED,
		"Submission Received",
		"Submitter",
		"SMS + Email",
		"Immediately on save",
		"Your grievance {{ ticket_number }} has been received under {{ service_category }}. "
		+ "Expected response by {{ sla_due_date }}.",
	),
	(
		C.EVENT_DUPLICATE_DETECTED,
		"Duplicate Detected",
		"Submitter",
		"SMS + Email",
		"On validation",
		"A similar grievance already exists. Reference {{ ticket_number }}. "
		+ "You may link to it or proceed with justification.",
	),
	(
		C.EVENT_ASSIGNED_AUTO,
		"Grievance Assigned (Auto)",
		"Department Officer",
		"Email",
		"On auto-routing match",
		"Grievance {{ ticket_number }} ({{ service_category }} / {{ grievance_type }}) has been "
		+ "assigned to {{ department }}. SLA deadline {{ sla_due_date }}.",
	),
	(
		C.EVENT_ASSIGNED_MANUAL,
		"Grievance Assigned (Manual)",
		"Department Officer",
		"Email",
		"On nodal officer assignment",
		"Grievance {{ ticket_number }} has been assigned to {{ department }} by the nodal officer. "
		+ "SLA deadline {{ sla_due_date }}.",
	),
	(
		C.EVENT_STATUS_IN_PROGRESS,
		"Status changed to In Progress",
		"Submitter",
		"SMS",
		"Officer accepts ticket",
		"Your grievance {{ ticket_number }} is now being handled by {{ department }}.",
	),
	(
		C.EVENT_MORE_INFO_REQUESTED,
		"More Information Requested",
		"Submitter",
		"SMS + Email",
		"Officer sets More Info Needed",
		"Additional information is needed for grievance {{ ticket_number }}. "
		+ "Please respond via the portal.",
	),
	(
		C.EVENT_SUBMITTER_RESPONDED,
		"Submitter Responded",
		"Department Officer",
		"Email",
		"Submitter provides info",
		"The submitter has responded on grievance {{ ticket_number }}.",
	),
	(
		C.EVENT_RESPONSE_SENT,
		"Structured Response Sent",
		"Submitter",
		"SMS + Email",
		"Officer submits response",
		"A response has been issued on grievance {{ ticket_number }}. "
		+ "Please confirm or reopen within the confirmation window.",
	),
	(
		C.EVENT_CONFIRMATION_WINDOW,
		"Confirmation Window Open",
		"Submitter",
		"SMS",
		"Response submitted",
		"Grievance {{ ticket_number }} is awaiting your confirmation. "
		+ "Confirm or reopen before the window closes.",
	),
	(
		C.EVENT_CONFIRMED,
		"Grievance Confirmed",
		"Submitter",
		"SMS + Email",
		"Submitter confirms",
		"Grievance {{ ticket_number }} has been marked resolved. "
		+ "Please rate your experience from 1 to 5.",
	),
	(
		C.EVENT_REOPENED,
		"Grievance Reopened",
		"Department Officer",
		"Email",
		"Submitter reopens",
		"Grievance {{ ticket_number }} has been reopened by the submitter and needs attention.",
	),
	(
		C.EVENT_AUTO_CLOSED,
		"Auto-closed (No Response)",
		"Submitter",
		"SMS + Email",
		"Confirmation window expires",
		"Grievance {{ ticket_number }} has been closed as no objection was received.",
	),
	(
		C.EVENT_CLOSED,
		"Grievance Closed",
		"Submitter",
		"SMS + Email",
		"On final closure",
		"Grievance {{ ticket_number }} is now closed. Thank you for your feedback.",
	),
	(
		C.EVENT_SLA_REMINDER_50,
		"SLA Reminder 50%",
		"Assigned Officer",
		"Email",
		"Scheduled job at 50% SLA elapsed",
		"Grievance {{ ticket_number }} has reached 50% of its SLA window. " + "Deadline {{ sla_due_date }}.",
	),
	(
		C.EVENT_SLA_REMINDER_80,
		"SLA Reminder 80%",
		"Assigned Officer",
		"Email",
		"Scheduled job at 80% SLA elapsed",
		"Grievance {{ ticket_number }} has reached 80% of its SLA window. Urgent action required.",
	),
	(
		C.EVENT_SLA_AT_RISK,
		"SLA At-risk Report",
		"Nodal Officer",
		"Email",
		"Scheduled job at 80%",
		"Grievance {{ ticket_number }} is approaching its SLA deadline {{ sla_due_date }}.",
	),
	(
		C.EVENT_SLA_BREACH_L1,
		"SLA Breached - L1",
		"Department Head",
		"Email",
		"SLA deadline passed",
		"Grievance {{ ticket_number }} has breached its SLA and has been escalated. "
		+ "Immediate action is required.",
	),
	(
		C.EVENT_SLA_BREACH_L2,
		"SLA Breached - L2",
		"Top Level Authority",
		"Email",
		"2x SLA deadline passed",
		"Grievance {{ ticket_number }} has breached twice its SLA window and is escalated "
		+ "to second level.",
	),
	(
		C.EVENT_MANUAL_ESCALATION,
		"Manual Escalation",
		"Department Head",
		"Email",
		"Submitter triggers escalation",
		"Grievance {{ ticket_number }} has been escalated by the submitter.",
	),
	(
		C.EVENT_REASSIGNMENT_REQUESTED,
		"Reassignment Requested",
		"Nodal Officer",
		"Email",
		"Officer requests reassign",
		"A reassignment has been requested on grievance {{ ticket_number }} and needs approval.",
	),
]


def after_install():
	seed_all()


def after_migrate():
	seed_all()


def seed_all():
	created = {
		"roles": seed_roles(),
		"categories": seed_categories(),
		"submitter_types": seed_submitter_types(),
		"submission_types": seed_submission_types(),
		"notifications": seed_notification_configs(),
		"administrative_areas": seed_administrative_areas(),
	}
	# Explicit commit after running setup seed data in after_install/after_migrate hook
	frappe.db.commit()  # nosemgrep
	return created


def seed_administrative_areas():
	"""Seed pre-calculated Ethiopian administrative area tree (21,028 nodes) from SQL seed if empty."""
	if frappe.db.count("Administrative Area") > 0:
		return 0

	sql_path = os.path.join(os.path.dirname(__file__), "data", "ethiopia_administrative_areas.sql.gz")
	if not os.path.exists(sql_path):
		return 0

	with gzip.open(sql_path, "rt", encoding="utf-8") as f:
		sql_content = f.read()

	statements = [s.strip() for s in sql_content.split(";\n") if s.strip() and not s.strip().startswith("--")]
	for statement in statements:
		frappe.db.sql(statement)  # nosemgrep

	return frappe.db.count("Administrative Area")


def seed_roles():
	made = []
	for role, desk_access in ROLES:
		if frappe.db.exists("Role", role):
			continue
		frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": desk_access}).insert(
			ignore_permissions=True
		)
		made.append(role)
	return made


def seed_categories():
	made = []
	for name, code, order in SERVICE_CATEGORIES:
		if frappe.db.exists("Service Category", name):
			continue
		frappe.get_doc(
			{
				"doctype": "Service Category",
				"category_name": name,
				"code": code,
				"sort_order": order,
				"is_active": 1,
			}
		).insert(ignore_permissions=True)
		made.append(name)
	return made


def seed_submitter_types():
	made = []
	for name, code in SUBMITTER_TYPES:
		if frappe.db.exists("Submitter Type", name):
			continue
		frappe.get_doc(
			{
				"doctype": "Submitter Type",
				"type_name": name,
				"code": code,
				"is_active": 1,
			}
		).insert(ignore_permissions=True)
		made.append(name)
	return made


def seed_submission_types():
	made = []
	for name, code in SUBMISSION_TYPES:
		if frappe.db.exists("Submission Type", name):
			continue
		frappe.get_doc(
			{
				"doctype": "Submission Type",
				"submission_type_name": name,
				"code": code,
				"is_active": 1,
			}
		).insert(ignore_permissions=True)
		made.append(name)
	return made


def seed_notification_configs():
	made = []
	for code, title, recipient, channel, trigger, template in NOTIFICATION_EVENTS:
		if frappe.db.exists("Grievance Notification Config", code):
			continue
		frappe.get_doc(
			{
				"doctype": "Grievance Notification Config",
				"event_code": code,
				"event_title": title,
				"recipient": recipient,
				"channel": channel,
				"trigger_description": trigger,
				"subject": title,
				"message_template": template,
				"language": "Both",
				"is_enabled": 1,
			}
		).insert(ignore_permissions=True)
		made.append(code)
	return made
