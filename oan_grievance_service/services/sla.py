"""FR-07 SLA and Escalation Management.

Reminders at 50% and 80% of the window, escalation on breach, second-level escalation
at 2x, and submitter-triggered manual escalation once the window has elapsed.

CLOCK START — a resolved specification conflict, recorded here rather than buried.
FSD 4.2 step 1 says "an SLA timer starts from the moment of assignment". FSD UC-04
step 1 computes the breach from "creation_date + sla_days". They differ for every
grievance that waits in the nodal officer's manual routing queue, which is exactly the
population most at risk of breaching.

This implementation follows FSD 4.2 (assignment), because the SLA is defined in 1.3 as
"the expected timeframe within which an assigned agency must act" — a department cannot
be held to a clock that ran before the case reached it. The behaviour is switchable via
the `grievance_sla_clock_start` site config key ("assignment" or "creation") so the
decision can be reversed without a code change.
"""

import frappe
from frappe import _
from frappe.utils import add_days, get_datetime, now_datetime

from oan_grievance_service.services import constants as C

CLOCK_START_ASSIGNMENT = "assignment"
CLOCK_START_CREATION = "creation"


def clock_start_mode():
	return frappe.conf.get("grievance_sla_clock_start") or CLOCK_START_ASSIGNMENT


def resolve_policy(service_category, grievance_type=None):
	"""Most-specific-first: an exact grievance type beats the category default."""
	if grievance_type:
		exact = frappe.get_all(
			"Grievance SLA Configuration",
			filters={
				"service_category": service_category,
				"grievance_type": grievance_type,
				"active": 1,
			},
			fields=["name", "sla_days", "top_level_authority", "auto_escalation_threshold"],
			limit=1,
		)
		if exact:
			return exact[0]

	default = frappe.get_all(
		"Grievance SLA Configuration",
		filters={
			"service_category": service_category,
			"grievance_type": ["is", "not set"],
			"active": 1,
		},
		fields=["name", "sla_days", "top_level_authority", "auto_escalation_threshold"],
		limit=1,
	)
	return default[0] if default else None


def start_clock(grievance):
	"""Stamp the SLA window onto the grievance. Idempotent."""
	if grievance.sla_due_date:
		return

	policy = resolve_policy(grievance.service_category, grievance.grievance_type)
	if not policy or not policy.sla_days:
		return

	if clock_start_mode() == CLOCK_START_CREATION:
		started = get_datetime(grievance.creation)
	else:
		started = now_datetime()

	grievance.db_set("sla_days", policy.sla_days, update_modified=False)
	grievance.db_set("sla_start_at", started, update_modified=False)
	grievance.db_set("sla_due_date", add_days(started, policy.sla_days), update_modified=False)


def consumed_percent(grievance):
	"""FSD 3.11.4: the SLA tracker's consumed percentage."""
	if not (grievance.sla_start_at and grievance.sla_due_date):
		return 0
	start = get_datetime(grievance.sla_start_at)
	due = get_datetime(grievance.sla_due_date)
	window = (due - start).total_seconds()
	if window <= 0:
		return 100
	elapsed = (now_datetime() - start).total_seconds()
	return max(0, min(round(elapsed / window * 100), 999))


def extend_for_deferral(grievance, additional_days):
	"""FSD 3.11.7: an approved deferral pushes the due date out."""
	if not grievance.sla_due_date:
		return
	grievance.db_set(
		"sla_due_date",
		add_days(get_datetime(grievance.sla_due_date), additional_days),
		update_modified=False,
	)
	grievance.db_set(
		"sla_deferred_days",
		(grievance.sla_deferred_days or 0) + additional_days,
		update_modified=False,
	)
	# The window moved, so the old reminders are no longer the right ones to suppress.
	grievance.db_set("reminder_50_sent", 0, update_modified=False)
	grievance.db_set("reminder_80_sent", 0, update_modified=False)


ESCALATION_ROLE_LEVELS = {
	"L1": "nodal_officer",
	"L2": "senior_nodal_officer",
	"L3": "department_head",
}


def escalate(grievance, level, trigger, reason=None, escalated_by=None):
	"""FSD 3.7: set the overlay flag, raise priority, log, and notify.

	Escalated is a flag, never a status, so the lifecycle stage is left untouched.
	"""
	from oan_grievance_service.permissions import find_officer_by_role_level
	from oan_grievance_service.services import notifications

	if already_escalated_at(grievance.name, level):
		return None

	policy = resolve_policy(grievance.service_category, grievance.grievance_type)
	target = policy.top_level_authority if (level == "L2" and policy and policy.top_level_authority) else None

	if not target:
		role_level = ESCALATION_ROLE_LEVELS.get(level, "nodal_officer")
		target = find_officer_by_role_level(
			role_level,
			department=grievance.assigned_dept,
			administrative_area=grievance.administrative_area,
		)

	if not target and grievance.assigned_dept:
		dept_field = "senior_officer" if level == "L2" else "nodal_officer"
		target = frappe.db.get_value(
			"Grievance Department", grievance.assigned_dept, dept_field
		) or frappe.db.get_value("Grievance Department", grievance.assigned_dept, "head_of_dept")

	log = frappe.get_doc(
		{
			"doctype": "Grievance Escalation Log",
			"grievance": grievance.name,
			"escalation_level": level,
			"triggered_at": now_datetime(),
			"triggered_by": trigger,
			"reason": reason,
			"notified_stakeholders": target or "",
		}
	).insert(ignore_permissions=True)

	grievance.db_set("escalated", 1, update_modified=False)
	grievance.db_set("escalation_level", 2 if level == "L2" else 1, update_modified=False)
	grievance.db_set("priority", "High", update_modified=False)

	notifications.queue(
		grievance,
		C.EVENT_SLA_BREACH_L2 if level == "L2" else C.EVENT_SLA_BREACH_L1,
		recipient_override=target,
	)
	return log


def already_escalated_at(grievance_name, level):
	return bool(
		frappe.db.exists("Grievance Escalation Log", {"grievance": grievance_name, "escalation_level": level})
	)


def clear_escalation(grievance):
	"""FSD 4.3: once a structured response is submitted the flag is cleared."""
	if not grievance.escalated:
		return
	grievance.db_set("escalated", 0, update_modified=False)
	frappe.db.set_value(
		"Grievance Escalation Log",
		{"grievance": grievance.name, "resolved_at": ["is", "not set"]},
		"resolved_at",
		now_datetime(),
		update_modified=False,
	)


def manual_escalate(grievance, reason, by_submitter=True):
	"""FSD 3.7: the submitter may escalate once the SLA window has elapsed."""
	if not grievance.sla_due_date:
		frappe.throw(_("This grievance has no SLA window yet, so it cannot be escalated."))
	if get_datetime(grievance.sla_due_date) > now_datetime():
		frappe.throw(_("The SLA window has not elapsed yet, so escalation is not available."))

	return escalate(
		grievance,
		level="L1",
		trigger="Submitter" if by_submitter else "Officer",
		reason=reason,
	)
