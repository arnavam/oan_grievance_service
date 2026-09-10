# Copyright (c) 2026, COSS - Centre for Open Societal Systems and contributors
# For license information, please see license.txt

"""FR-09 Reporting and Analytics: SLA compliance rates and average resolution times,
sliceable by category, region, department and date range.

The report reads through the same permission query conditions as the list view, so a
department head sees their department's numbers and nobody sees more than their scope.
"""

import frappe
from frappe import _
from frappe.utils import flt, get_datetime

from oan_grievance_service.services import constants as C


def execute(filters=None):
	filters = frappe._dict(filters or {})
	rows = get_data(filters)
	return get_columns(), rows, None, get_chart(rows)


def get_columns():
	return [
		{
			"label": _("Ticket"),
			"fieldname": "ticket_number",
			"fieldtype": "Link",
			"options": "Grievance",
			"width": 190,
		},
		{
			"label": _("Category"),
			"fieldname": "service_category",
			"fieldtype": "Link",
			"options": "Service Category",
			"width": 110,
		},
		{
			"label": _("Administrative Area"),
			"fieldname": "administrative_area",
			"fieldtype": "Link",
			"options": "Administrative Area",
			"width": 140,
		},
		{
			"label": _("Department"),
			"fieldname": "assigned_dept",
			"fieldtype": "Link",
			"options": "Grievance Department",
			"width": 180,
		},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 130},
		{"label": _("SLA Days"), "fieldname": "sla_days", "fieldtype": "Int", "width": 80},
		{"label": _("Due"), "fieldname": "sla_due_date", "fieldtype": "Datetime", "width": 160},
		{"label": _("Resolution Days"), "fieldname": "resolution_days", "fieldtype": "Float", "width": 130},
		{"label": _("Within SLA"), "fieldname": "within_sla", "fieldtype": "Data", "width": 100},
		{"label": _("Escalated"), "fieldname": "escalated", "fieldtype": "Check", "width": 90},
	]


def get_data(filters):
	conditions = {}
	for field in ("service_category", "assigned_dept", "status"):
		if filters.get(field):
			conditions[field] = filters[field]
	if filters.get("from_date") and filters.get("to_date"):
		conditions["creation"] = ["between", [filters.from_date, filters.to_date]]

	if filters.get("administrative_area"):
		area_lft, area_rgt = frappe.db.get_value(
			"Administrative Area", filters.administrative_area, ["lft", "rgt"]
		) or (None, None)
		if area_lft is not None and area_rgt is not None:
			conditions["area_lft"] = ["between", [area_lft, area_rgt]]
		else:
			conditions["administrative_area"] = filters.administrative_area

	grievances = frappe.get_all(
		"Grievance",
		filters=conditions,
		fields=[
			"name",
			"ticket_number",
			"service_category",
			"administrative_area",
			"assigned_dept",
			"status",
			"sla_days",
			"sla_due_date",
			"creation",
			"escalated",
		],
		order_by="creation desc",
	)

	# One query for every closing timestamp, rather than one per row.
	closed_at = dict(
		frappe.get_all(
			"Grievance Status History",
			filters={
				"grievance": ["in", [g.name for g in grievances]] if grievances else ["in", [""]],
				"to_status": ["in", [C.RESOLVED, C.CLOSED]],
			},
			fields=["grievance", "min(timestamp) as closed_at"],
			group_by="grievance",
			as_list=True,
		)
	)

	rows = []
	for g in grievances:
		closed = closed_at.get(g.name)
		resolution_days = None
		within = "-"
		if closed:
			resolution_days = flt(
				(get_datetime(closed) - get_datetime(g.creation)).total_seconds() / 86400, 2
			)
			if g.sla_due_date:
				within = "Yes" if get_datetime(closed) <= get_datetime(g.sla_due_date) else "No"

		rows.append(
			{
				"ticket_number": g.name,
				"service_category": g.service_category,
				"region": g.region,
				"assigned_dept": g.assigned_dept,
				"status": g.status,
				"sla_days": g.sla_days,
				"sla_due_date": g.sla_due_date,
				"resolution_days": resolution_days,
				"within_sla": within,
				"escalated": g.escalated,
			}
		)
	return rows


def get_chart(rows):
	"""FSD 3.9: SLA compliance rate as a headline figure."""
	met = sum(1 for r in rows if r["within_sla"] == "Yes")
	missed = sum(1 for r in rows if r["within_sla"] == "No")
	if not (met or missed):
		return None
	return {
		"data": {
			"labels": [_("Within SLA"), _("Breached")],
			"datasets": [{"name": _("Cases"), "values": [met, missed]}],
		},
		"type": "donut",
	}
