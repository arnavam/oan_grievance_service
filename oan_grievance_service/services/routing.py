"""FR-03 Routing and Assignment with Nearest-Ancestor Administrative Area matching.

Auto-routing rules consider service category, grievance type, administrative area (tree hierarchy)
and associated service provider. Where a rule matches, the grievance is assigned and the
status advances to Assigned. Where none matches, it stays Submitted and sits in the
nodal officer's manual queue.
"""

import frappe

from oan_grievance_service.services import constants as C

MATCH_FIELDS = (
	("service_category", "service_category"),
	("grievance_type", "grievance_type"),
	("service_provider", "associated_service_provider"),
)


def find_matching_rule(grievance):
	"""Return the winning Grievance Routing Rule, or None for the manual queue.

	Uses Nearest-Ancestor resolution for administrative_area:
	A rule matches when its category, type, and provider match (or are unconstrained),
	and its administrative_area is an ancestor or exact match of the grievance's area.
	Among matching rules:
	1. Explicit rule_precedence (lower is evaluated first)
	2. Narrowest tree span (rgt - lft) = deepest/nearest ancestor
	3. Specificity count (number of constrained dimensions)
	"""
	case_area = grievance.get("administrative_area")
	case_lft = grievance.get("area_lft")
	case_rgt = None
	if case_area and case_lft is None:
		case_lft, case_rgt = frappe.db.get_value("Administrative Area", case_area, ["lft", "rgt"]) or (
			None,
			None,
		)
	elif case_area and case_lft is not None:
		case_rgt = frappe.db.get_value("Administrative Area", case_area, "rgt")

	rules = frappe.get_all(
		"Grievance Routing Rule",
		filters={"active": 1},
		fields=[
			"name",
			"rule_precedence",
			"assigned_dept",
			"priority",
			"administrative_area",
			*[rule_field for rule_field, _ in MATCH_FIELDS],
		],
		order_by="rule_precedence asc",
	)

	candidates = []
	for rule in rules:
		specificity = 0
		matched = True

		# Direct fields match
		for rule_field, doc_field in MATCH_FIELDS:
			constraint = rule.get(rule_field)
			if not constraint:
				continue
			specificity += 1
			if constraint != grievance.get(doc_field):
				matched = False
				break
		if not matched:
			continue

		# Administrative Area nearest-ancestor containment check
		rule_area = rule.get("administrative_area")
		area_span = 999999999  # Global default (no area constraint)
		if rule_area:
			specificity += 1
			if not case_area or case_lft is None:
				matched = False
			else:
				rule_lft, rule_rgt = frappe.db.get_value(
					"Administrative Area", rule_area, ["lft", "rgt"]
				) or (None, None)
				if rule_lft is None or rule_rgt is None:
					matched = False
				elif not (rule_lft <= int(case_lft) and rule_rgt >= int(case_rgt or case_lft)):
					matched = False
				else:
					area_span = int(rule_rgt) - int(rule_lft)

		if matched:
			# Sorting tuple: (rule_precedence, area_span, -specificity)
			candidates.append((rule.rule_precedence or 0, area_span, -specificity, rule))

	if not candidates:
		return None
	candidates.sort(key=lambda row: (row[0], row[1], row[2]))
	return candidates[0][3]


def apply_routing(grievance, commit_status=True):
	"""Route a grievance. Returns the rule that matched, or None.

	FSD 3.3: on a match the department is set and status advances to Assigned. With no
	match the status stays Submitted so the case surfaces in the manual routing queue.
	"""
	from oan_grievance_service.services import lifecycle, notifications

	rule = find_matching_rule(grievance)

	if not rule:
		grievance.db_set("routed_automatically", 0, update_modified=False)
		return None

	grievance.db_set("assigned_dept", rule.assigned_dept, update_modified=False)
	grievance.db_set("routing_rule", rule.name, update_modified=False)
	grievance.db_set("routed_automatically", 1, update_modified=False)
	if rule.priority:
		grievance.db_set("priority", rule.priority, update_modified=False)

	if commit_status:
		lifecycle.change_status(
			grievance,
			C.ASSIGNED,
			note=f"Auto-routed by rule {rule.name}",
			automated=True,
		)
		notifications.queue(grievance, C.EVENT_ASSIGNED_AUTO)

	return rule


def manual_assign(grievance, department, officer=None, assigned_by=None):
	"""FSD 3.3 / 4.1 step 8b: the nodal officer assigns from the manual queue."""
	from oan_grievance_service.services import lifecycle, notifications

	grievance.db_set("assigned_dept", department, update_modified=False)
	if officer:
		grievance.db_set("assigned_to", officer, update_modified=False)
	grievance.db_set("routed_automatically", 0, update_modified=False)

	lifecycle.change_status(
		grievance,
		C.ASSIGNED,
		note=f"Manually assigned by {assigned_by or frappe.session.user}",
	)
	notifications.queue(grievance, C.EVENT_ASSIGNED_MANUAL)


def manual_queue():
	"""FSD 3.3: grievances awaiting a nodal officer's routing decision."""
	return frappe.get_all(
		"Grievance",
		filters={"status": C.SUBMITTED, "assigned_dept": ["is", "not set"]},
		fields=["name", "ticket_number", "service_category", "administrative_area", "creation"],
		order_by="creation asc",
	)
