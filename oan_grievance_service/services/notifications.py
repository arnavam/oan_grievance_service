"""FR-08 Notifications and Communication, per the Appendix C matrix.

Every event is a Grievance Notification Config record, so an administrator can change
the channel, recipient, wording or enable state without a release (FSD 3.11.7).

The Appendix C closing note requires controls against redundant messaging, so a queued
row is deduplicated on (grievance, event, recipient) and a disabled config sends nothing.
"""

import frappe
from frappe.utils import now_datetime

from oan_grievance_service.services import constants as C

RECIPIENT_SUBMITTER = "Submitter"
RECIPIENT_ASSIGNED_OFFICER = "Assigned Officer"
RECIPIENT_DEPARTMENT_OFFICER = "Department Officer"
RECIPIENT_NODAL_OFFICER = "Nodal Officer"
RECIPIENT_DEPARTMENT_HEAD = "Department Head"
RECIPIENT_TOP_LEVEL = "Top Level Authority"


def resolve_recipient(grievance, recipient_type, override=None):
	"""Turn a recipient role into an actual address at send time.

	Addresses are never stored on the config, so a staff change does not misdirect
	notifications on historic cases.
	"""
	if override:
		return override

	if recipient_type == RECIPIENT_SUBMITTER:
		return grievance.contact_email or grievance.contact_mobile

	if recipient_type == RECIPIENT_ASSIGNED_OFFICER:
		return grievance.assigned_to

	if not grievance.assigned_dept:
		return None

	dept = frappe.db.get_value(
		"Grievance Department",
		grievance.assigned_dept,
		["email_account", "head_of_dept", "nodal_officer", "senior_officer"],
		as_dict=True,
	)
	if not dept:
		return None

	return {
		RECIPIENT_DEPARTMENT_OFFICER: dept.email_account,
		RECIPIENT_DEPARTMENT_HEAD: dept.head_of_dept,
		RECIPIENT_NODAL_OFFICER: dept.nodal_officer,
		RECIPIENT_TOP_LEVEL: dept.senior_officer,
	}.get(recipient_type)


def render(template, grievance):
	"""Interpolate the {{ field }} placeholders FSD 3.11.7 calls for."""
	if not template:
		return ""
	context = {
		"ticket_number": grievance.ticket_number or grievance.name,
		"submitter_name": grievance.submitter_name or "",
		"service_category": grievance.service_category or "",
		"grievance_type": grievance.grievance_type or "",
		"status": grievance.status or "",
		"department": grievance.assigned_dept or "",
		"sla_due_date": grievance.sla_due_date or "",
		"administrative_area": getattr(grievance, "administrative_area", "") or "",
		"administrative_unit": getattr(grievance, "administrative_unit", "") or "",
		"region": getattr(grievance, "administrative_area", "") or "",
		"woreda": getattr(grievance, "administrative_unit", "") or "",
	}
	rendered = template
	for key, value in context.items():
		rendered = rendered.replace("{{ %s }}" % key, str(value))
		rendered = rendered.replace("{{%s}}" % key, str(value))
	return rendered


def queue(grievance, event_code, recipient_override=None):
	"""Write a Grievance Notification Log row for an Appendix C event.

	Returns the log rows created, which is an empty list when the event is disabled
	or has already been sent for this grievance.
	"""
	config = frappe.db.get_value(
		"Grievance Notification Config",
		{"event_code": event_code, "is_enabled": 1},
		["name", "event_title", "recipient", "channel", "subject", "message_template", "language"],
		as_dict=True,
	)
	if not config:
		return []

	recipient = resolve_recipient(grievance, config.recipient, recipient_override)
	if not recipient:
		return []

	# Appendix C note: guard against redundant messaging.
	if frappe.db.exists(
		"Grievance Notification Log",
		{"grievance": grievance.name, "event": event_code, "recipient": recipient},
	):
		return []

	channels = ["SMS", "Email"] if config.channel == "SMS + Email" else [config.channel]
	message = render(config.message_template, grievance)

	rows = []
	for channel in channels:
		row = frappe.get_doc(
			{
				"doctype": "Grievance Notification Log",
				"grievance": grievance.name,
				"event": event_code,
				"recipient": recipient,
				"channel": channel,
				"message": message,
				"status": "Queued",
				"language": "English" if config.language == "Both" else config.language,
			}
		).insert(ignore_permissions=True)
		rows.append(row)

	return rows


def dispatch_queued(limit=100):
	"""Send whatever is queued. Called by the scheduler.

	The gateway integrations named in FSD section 6 are not built yet, so this marks
	rows Sent and records the attempt. Wire the SMS gateway and email account here.
	"""
	pending = frappe.get_all(
		"Grievance Notification Log",
		filters={"status": "Queued"},
		fields=["name", "channel", "recipient", "message"],
		limit=limit,
	)

	for row in pending:
		try:
			if row.channel == "Email" and "@" in (row.recipient or ""):
				frappe.sendmail(
					recipients=[row.recipient],
					subject="Grievance update",
					message=row.message,
					delayed=True,
				)
			# SMS: FSD section 6 names an external gateway. Integration point.
			frappe.db.set_value(
				"Grievance Notification Log",
				row.name,
				{"status": "Sent", "sent_at": now_datetime()},
				update_modified=False,
			)
		except Exception:
			frappe.log_error(
				title="Grievance notification dispatch failed",
				message=frappe.get_traceback(),
			)
			frappe.db.set_value(
				"Grievance Notification Log",
				row.name,
				"status",
				"Failed",
				update_modified=False,
			)

	return len(pending)
