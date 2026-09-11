"""FR-04 Tracking and Status Management.

Every status change goes through change_status, which refuses an illegal move, writes
the Grievance Status History row, and fires the matching notification. Nothing sets
`status` directly, so the trail cannot be bypassed.
"""

import frappe
from frappe import _
from frappe.utils import add_days, now_datetime

from oan_grievance_service.services import constants as C


def change_status(grievance, to_status, note=None, reason=None, automated=False, notify=True):
	"""Move a grievance to a new status, recording history. Returns the history row."""
	from oan_grievance_service.services import notifications, sla

	from_status = grievance.status
	if from_status == to_status:
		return None

	allowed = C.ALLOWED_TRANSITIONS.get(from_status, set())
	if to_status not in allowed:
		frappe.throw(
			_("Cannot move a grievance from {0} to {1}.").format(
				frappe.bold(from_status), frappe.bold(to_status)
			),
			title=_("Illegal Status Transition"),
		)

	if to_status in C.REASON_REQUIRED_TO and not reason:
		frappe.throw(
			_("A reason is required to set status {0}.").format(frappe.bold(to_status)),
			title=_("Reason Required"),
		)

	grievance.db_set("status", to_status, update_modified=False)

	history = frappe.get_doc(
		{
			"doctype": "Grievance Status History",
			"grievance": grievance.name,
			"from_status": from_status,
			"to_status": to_status,
			"changed_by": frappe.session.user,
			"timestamp": now_datetime(),
			"notes": reason or note,
		}
	).insert(ignore_permissions=True)

	# FSD 4.2 step 1: the SLA clock starts when the case reaches a department.
	if to_status == C.ASSIGNED:
		sla.start_clock(grievance)

	# FSD 3.6: entering Pending Submitter opens the confirmation window.
	if to_status == C.PENDING_SUBMITTER:
		grievance.db_set(
			"confirmation_deadline",
			add_days(now_datetime(), confirmation_window_days()),
			update_modified=False,
		)

	if notify:
		event = STATUS_EVENT.get(to_status)
		if event:
			notifications.queue(grievance, event)

	return history


STATUS_EVENT = {
	C.IN_PROGRESS: C.EVENT_STATUS_IN_PROGRESS,
	C.MORE_INFO_NEEDED: C.EVENT_MORE_INFO_REQUESTED,
	C.PENDING_SUBMITTER: C.EVENT_CONFIRMATION_WINDOW,
	C.RESOLVED: C.EVENT_CONFIRMED,
	C.CLOSED: C.EVENT_CLOSED,
}


def confirmation_window_days():
	"""FSD 3.6: configurable, default 7 days."""
	return frappe.conf.get("grievance_confirmation_window_days") or C.DEFAULT_CONFIRMATION_DAYS


def accept(grievance):
	"""FSD 4.2 step 2: the officer accepts and begins work."""
	return change_status(grievance, C.IN_PROGRESS, note="Accepted by department officer")


def request_more_info(grievance, question):
	"""FSD 4.2 step 3: the officer asks the submitter for more detail."""
	from oan_grievance_service.services import notifications

	comment = frappe.get_doc(
		{
			"doctype": "Grievance Comment",
			"grievance": grievance.name,
			"comment_type": "Information Request",
			"is_internal": 0,
			"body": question,
			"author_user": frappe.session.user,
			"created_on": now_datetime(),
		}
	).insert(ignore_permissions=True)

	change_status(grievance, C.MORE_INFO_NEEDED, note="Additional information requested")
	notifications.queue(grievance, C.EVENT_MORE_INFO_REQUESTED)
	return comment


def submitter_replies(grievance, body):
	"""FSD Appendix C: the submitter answers, the case returns to In Progress."""
	from oan_grievance_service.services import notifications

	comment = frappe.get_doc(
		{
			"doctype": "Grievance Comment",
			"grievance": grievance.name,
			"comment_type": "Information Response",
			"is_internal": 0,
			"body": body,
			"author_submitter": grievance.submitter,
			"created_on": now_datetime(),
		}
	).insert(ignore_permissions=True)

	if grievance.status == C.MORE_INFO_NEEDED:
		change_status(grievance, C.IN_PROGRESS, note="Submitter provided the requested information")
	notifications.queue(grievance, C.EVENT_SUBMITTER_RESPONDED)
	return comment


def confirm_resolution(grievance):
	"""FSD 3.6 / UC-03: the submitter confirms, so the case resolves then closes."""
	change_status(grievance, C.RESOLVED, note="Confirmed by submitter")
	grievance.db_set("closure_reason", "Confirmed by submitter", update_modified=False)
	return change_status(grievance, C.CLOSED, note="Auto-closed after submitter confirmation")


def reopen(grievance, reason):
	"""FSD 3.6: reopening requires a mandatory reason."""
	from oan_grievance_service.services import notifications

	if not reason:
		frappe.throw(_("A reason is required to reopen a grievance."), title=_("Reason Required"))

	grievance.db_set("reopen_count", (grievance.reopen_count or 0) + 1, update_modified=False)
	history = change_status(grievance, C.REOPEN_TARGET, reason=reason, notify=False)
	notifications.queue(grievance, C.EVENT_REOPENED)
	return history


def auto_close(grievance):
	"""FSD 3.6: no response inside the confirmation window closes the case."""
	from oan_grievance_service.services import notifications

	grievance.db_set("closure_reason", "Closed - no objection received", update_modified=False)
	history = change_status(
		grievance, C.CLOSED, note="Closed - no objection received", automated=True, notify=False
	)
	notifications.queue(grievance, C.EVENT_AUTO_CLOSED)
	return history


def reject(grievance, reason):
	"""FSD 3.4: a terminal state for an invalid or out-of-scope grievance."""
	return change_status(grievance, C.REJECTED, reason=reason)


def display_group_filters(group):
	"""FSD 3.11.3: map a work-queue card to canonical statuses."""
	statuses = C.DISPLAY_GROUPS.get(group)
	if statuses is None:
		return {}
	return {"status": ["in", list(statuses)]}
