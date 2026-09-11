"""Seed data created on install and refreshed on migrate.

Everything here is configuration the FSD names explicitly, so a fresh site comes up
usable rather than empty: the three capability roles, the Appendix F escalation levels,
the five service categories from 3.2.2, the Ethiopian regions from 3.11.8, and the full
Appendix C notification matrix.
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

# The escalation rungs of §10.1, as Grievance Role Level master records.
#
# §10.1 retired `L1 Nodal Officer`, `L2 Senior Nodal Officer` and `Department Head` as
# *roles* - seniority is not a capability - but the rungs themselves remain, because
# escalation and remand still have to name where a case is going. They are three, and
# they map 1:1 onto the Grievance Department slots that notifications.py and sla.py
# already read:
#
#     nodal_officer         -> Grievance Department.nodal_officer
#     senior_nodal_officer  -> Grievance Department.senior_officer
#     department_head       -> Grievance Department.head_of_dept
#
# The older lists in database-schema.md §57 / §245 and verifier_management_schema_and_flow.md
# §93 also carried `l1_case_officer` and `l2_supervisor`. Those are not rungs of this
# organisation - in §10.1 the L1 / L2 prefixes qualify Nodal Officer, they do not name
# separate case-officer and supervisor tiers. They are dropped here.
#
# Orders run in tens so a rung can be inserted between two others without renumbering.
ROLE_LEVELS = [
	("nodal_officer", "Nodal Officer", 10, "L1. Department-wide coordination point for grievances."),
	(
		"senior_nodal_officer",
		"Senior Nodal Officer",
		20,
		"L2. Senior coordination; first rung above the nodal officer.",
	),
	("department_head", "Department Head", 30, "Final internal escalation rung for the department."),
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
#
# Each row becomes one core Notification record per channel, so an administrator can
# change the wording, channel, recipient or enable state from the desk without a
# release (FSD 3.11.7). "SMS + Email" in the FSD is two records here, because core's
# channel is a single Select.
#
# Message bodies are wrapped in _() so the same record serves both languages: the
# send path renders once per recipient inside print_language(), and the Amharic comes
# from core Translation records, which are themselves desk-editable. The context key
# pins the lookup, so rewording the English does not silently orphan the Amharic.
#
# (event_code, title, recipient_role, channels, trigger, source_text, format_args)
NOTIFICATION_EVENTS = [
	(
		C.EVENT_SUBMISSION_RECEIVED,
		"Submission Received",
		"Submitter",
		("SMS", "Email"),
		"Immediately on save",
		"Your grievance {0} has been received under {1}. Expected response by {2}.",
		("doc.ticket_number", "doc.service_category", "doc.sla_due_date"),
	),
	(
		C.EVENT_DUPLICATE_DETECTED,
		"Duplicate Detected",
		"Submitter",
		("SMS", "Email"),
		"On validation",
		"A similar grievance already exists. Reference {0}. You may link to it or proceed with justification.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_ASSIGNED_AUTO,
		"Grievance Assigned (Auto)",
		"Department Officer",
		("Email",),
		"On auto-routing match",
		"Grievance {0} ({1} / {2}) has been assigned to {3}. SLA deadline {4}.",
		(
			"doc.ticket_number",
			"doc.service_category",
			"doc.grievance_type",
			"doc.assigned_dept",
			"doc.sla_due_date",
		),
	),
	(
		C.EVENT_ASSIGNED_MANUAL,
		"Grievance Assigned (Manual)",
		"Department Officer",
		("Email",),
		"On nodal officer assignment",
		"Grievance {0} has been assigned to {1} by the nodal officer. SLA deadline {2}.",
		("doc.ticket_number", "doc.assigned_dept", "doc.sla_due_date"),
	),
	(
		C.EVENT_STATUS_IN_PROGRESS,
		"Status changed to In Progress",
		"Submitter",
		("SMS",),
		"Officer accepts ticket",
		"Your grievance {0} is now being handled by {1}.",
		("doc.ticket_number", "doc.assigned_dept"),
	),
	(
		C.EVENT_MORE_INFO_REQUESTED,
		"More Information Requested",
		"Submitter",
		("SMS", "Email"),
		"Officer sets More Info Needed",
		"Additional information is needed for grievance {0}. Please respond via the portal.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_SUBMITTER_RESPONDED,
		"Submitter Responded",
		"Department Officer",
		("Email",),
		"Submitter provides info",
		"The submitter has responded on grievance {0}.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_RESPONSE_SENT,
		"Structured Response Sent",
		"Submitter",
		("SMS", "Email"),
		"Officer submits response",
		"A response has been issued on grievance {0}. Please confirm or reopen within the confirmation window.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_CONFIRMATION_WINDOW,
		"Confirmation Window Open",
		"Submitter",
		("SMS",),
		"Response submitted",
		"Grievance {0} is awaiting your confirmation. Confirm or reopen before the window closes.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_CONFIRMED,
		"Grievance Confirmed",
		"Submitter",
		("SMS", "Email"),
		"Submitter confirms",
		"Grievance {0} has been marked resolved. Please rate your experience from 1 to 5.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_REOPENED,
		"Grievance Reopened",
		"Department Officer",
		("Email",),
		"Submitter reopens",
		"Grievance {0} has been reopened by the submitter and needs attention.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_AUTO_CLOSED,
		"Auto-closed (No Response)",
		"Submitter",
		("SMS", "Email"),
		"Confirmation window expires",
		"Grievance {0} has been closed as no objection was received.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_CLOSED,
		"Grievance Closed",
		"Submitter",
		("SMS", "Email"),
		"On final closure",
		"Grievance {0} is now closed. Thank you for your feedback.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_SLA_REMINDER_50,
		"SLA Reminder 50%",
		"Assigned Officer",
		("Email",),
		"Scheduled job at 50% SLA elapsed",
		"Grievance {0} has reached 50% of its SLA window. Deadline {1}.",
		("doc.ticket_number", "doc.sla_due_date"),
	),
	(
		C.EVENT_SLA_REMINDER_80,
		"SLA Reminder 80%",
		"Assigned Officer",
		("Email",),
		"Scheduled job at 80% SLA elapsed",
		"Grievance {0} has reached 80% of its SLA window. Urgent action required.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_SLA_AT_RISK,
		"SLA At-risk Report",
		"Nodal Officer",
		("Email",),
		"Scheduled job at 80%",
		"Grievance {0} is approaching its SLA deadline {1}.",
		("doc.ticket_number", "doc.sla_due_date"),
	),
	(
		C.EVENT_SLA_BREACH_L1,
		"SLA Breached - L1",
		"Department Head",
		("Email",),
		"SLA deadline passed",
		"Grievance {0} has breached its SLA and has been escalated. Immediate action is required.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_SLA_BREACH_L2,
		"SLA Breached - L2",
		"Top Level Authority",
		("Email",),
		"2x SLA deadline passed",
		"Grievance {0} has breached twice its SLA window and is escalated to second level.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_MANUAL_ESCALATION,
		"Manual Escalation",
		"Department Head",
		("Email",),
		"Submitter triggers escalation",
		"Grievance {0} has been escalated by the submitter.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_REASSIGNMENT_REQUESTED,
		"Reassignment Requested",
		"Nodal Officer",
		("Email",),
		"Officer requests reassign",
		"A reassignment has been requested on grievance {0} and needs approval.",
		("doc.ticket_number",),
	),
]


def after_install():
	seed_all()


def after_migrate():
	seed_all()


def seed_all():
	created = {
		"roles": seed_roles(),
		"role_levels": seed_role_levels(),
		"categories": seed_categories(),
		"submitter_types": seed_submitter_types(),
		"submission_types": seed_submission_types(),
		"notification_recipient_field": seed_recipient_custom_field(),
		"notifications": seed_notifications(),
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


def seed_role_levels():
	made = []
	for code, name, order, description in ROLE_LEVELS:
		if frappe.db.exists("Grievance Role Level", code):
			continue
		frappe.get_doc(
			{
				"doctype": "Grievance Role Level",
				"level_code": code,
				"level_name": name,
				"level_order": order,
				"description": description,
				"is_active": 1,
			}
		).insert(ignore_permissions=True)
		made.append(code)
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


def seed_recipient_custom_field():
	"""The recipient role, as a Select on core Notification.

	FSD 3.11.7 requires the recipient be changeable without a release, so it cannot be
	hardcoded per event in our Python. Core's own receiver_by_document_field is not
	usable for this: notification.js repopulates that Select's options with the target
	doctype's fieldnames, so a role token stored there would fall outside the option
	list and could be silently cleared the first time an admin opened the form.

	A Custom Field is Frappe's supported way to extend a core doctype - it is a record,
	not an edit to apps/frappe - and it gives admins a dropdown of exactly the six
	Appendix C roles.
	"""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_field

	from oan_grievance_service.services.notifications import RECIPIENT_ROLES

	if frappe.db.exists("Custom Field", "Notification-grievance_recipient"):
		return []

	create_custom_field(
		"Notification",
		{
			"fieldname": "grievance_recipient",
			"label": "Grievance Recipient",
			"fieldtype": "Select",
			"options": "\n".join(("", *RECIPIENT_ROLES)),
			"insert_after": "document_type",
			"depends_on": 'eval:doc.document_type=="Grievance"',
			"description": "FSD Appendix C recipient role. Resolved to a User at send time.",
		},
	)
	return ["Notification-grievance_recipient"]


def _translatable(source, args, context_key):
	"""Wrap an English source string as a translatable Jinja expression.

	The source string is the translation key, which is the one sharp edge of keeping
	both languages desk-editable from a single record: reword the English and the
	Amharic Translation stops matching. The context key is passed to _() so the lookup
	keys on something stable instead.
	"""
	call = f"_({source!r}, context={context_key!r})"
	if args:
		call += ".format(" + ", ".join(args) + ")"
	return "{{ " + call + " }}"


def seed_notifications():
	"""One core Notification per (Appendix C event, channel).

	Carried on core's "Method" trigger: the lifecycle fires these explicitly from the
	service layer rather than on Save or Value Change, so the event code is the method
	name and notifications.queue() looks them up by it.
	"""
	made = []
	for code, title, recipient, channels, _trigger, source, args in NOTIFICATION_EVENTS:
		for channel in channels:
			name = f"Grievance: {title} ({channel})"
			if frappe.db.exists("Notification", name):
				continue
			frappe.get_doc(
				{
					"doctype": "Notification",
					"name": name,
					# Translatable like the body: the subject is rendered for email and
					# reused as the in-app title, so a literal here reaches the
					# recipient just as untranslated as a literal in the message.
					"subject": _translatable(title, (), f"grievance.{code}.subject"),
					"document_type": "Grievance",
					"event": "Method",
					"method": code,
					"channel": channel,
					"grievance_recipient": recipient,
					"message": _translatable(source, args, f"grievance.{code}"),
					"message_type": "Plain Text",
					# Deliberately not is_standard. A standard Notification loads its
					# template from a file in the module folder and validate_standard()
					# refuses edits outside developer mode, which is the opposite of the
					# desk-editable wording FSD 3.11.7 asks for.
					"is_standard": 0,
					"enabled": 1,
				}
			).insert(ignore_permissions=True)
			made.append(name)
	return made
