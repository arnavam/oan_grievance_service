"""Role-based access control, deny-by-default.

The service runs on three capability roles and only three. A role answers *what actions
exist for you*; it never answers *which cases you may touch*. That second question is
answered by the Grievance RBAC Assignment records for the user - region, department and
category - applied as a permission query condition, so it filters list views, reports
and the API uniformly rather than being re-checked per screen.

A fourth role, Grievance Review Officer, is read-only oversight: it reads cases, officer
statistics and administration data. It holds no write DocPerm and is left out of every
mutating endpoint's `require_role` list, so Frappe's role permissions refuse it.

Seniority is deliberately absent from the role list. The former L1 / L2 / Department
Head roles were rungs of a hierarchy, not distinct capabilities, and are replaced by
position in the reporting chain. See
.docs/sla_workflows_and_lifecycle_specification.md §10.1.

Routing eligibility does not by itself grant edit rights: an
explicit case assignment or approval permission is required. That distinction is what
this module enforces.

What belongs here: access rules and their enforcement.
- Role constants.
- The Frappe hooks registered in hooks.py (`grievance_query_conditions`,
  `has_grievance_permission`).
- Predicates that answer "may this user see / do this", such as `outranks`.

What does not belong here:
- Data lookups (who holds which scope, which area contains which, which profiles a
  user owns). Those live in the module of the doctype they read, and this module
  imports them.
- Role checks written inline elsewhere. An API or controller asks a predicate here
  rather than inspecting `frappe.get_roles()` itself, so each rule has one copy.
"""

import frappe
from frappe import _

from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
	active_scopes,
	current_level_of,
	get_subordinate_officers,
)
from oan_grievance_service.grievance_management.doctype.grievance_submitter_profile.grievance_submitter_profile import (
	profiles_of,
)
from oan_grievance_service.grievance_masters.doctype.grievance_administrative_area.grievance_administrative_area import (
	area_bounds,
	is_in_area_subtree,
)
from oan_grievance_service.services.constants import (
	ROLE_ADMIN,
	ROLE_ASSISTED_SUBMITTER,
	ROLE_DESK_HEAD,
	ROLE_OFFICER,
	ROLE_REVIEW_OFFICER,
	ROLE_SUBMITTER,
	STAFF_ROLES,
)

GRIEVANCE_ROLES = (ROLE_ADMIN, ROLE_OFFICER, ROLE_ASSISTED_SUBMITTER, ROLE_SUBMITTER)

# An assisted submitter accesses their own cases and cases they assisted.
SUBMITTER_ROLES = {ROLE_SUBMITTER, ROLE_ASSISTED_SUBMITTER}

# Administrators see all regions, departments and categories.
UNRESTRICTED_ROLES = {ROLE_ADMIN, "System Manager", "Administrator"}


def session_user(user=None):
	"""The signed-in user, or None for Guest / unauthenticated sessions."""
	u = user or frappe.session.user
	return None if u in ("Guest", None) else u


def is_staff(user=None):
	"""Officers and administrators: anyone who works cases rather than files them."""
	return bool(set(frappe.get_roles(user or frappe.session.user)) & STAFF_ROLES)


def is_unrestricted(user=None):
	"""Administrators, who see every region, department and category."""
	return bool(set(frappe.get_roles(user or frappe.session.user)) & UNRESTRICTED_ROLES)


def can_see_identity(grievance, user=None):
	"""Whether the submitter's name and contact details may be shown to `user`.

	An anonymous case shows its identity only to administrators and to the submitter
	whose profile is on the case. The record's creator and an assisting officer do not
	count: filing on someone's behalf does not make their identity yours to see later.
	"""
	if not grievance.get("is_anonymous"):
		return True
	user = user or frappe.session.user
	if is_unrestricted(user):
		return True
	submitter = grievance.get("submitter")
	if not submitter or user == "Guest":
		return False
	return bool(frappe.db.exists("Grievance Submitter Profile", {"name": submitter, "user": user}))


def _quote(values):
	return ", ".join(frappe.db.escape(v) for v in values if v)


def _scope_clauses(scopes):
	"""SQL for the cases inside each of `scopes`, one parenthesised clause per scope."""
	clauses = []
	bounds = area_bounds(scopes)
	for scope in scopes:
		dept_scope = scope.get("department_scope")
		cat_scope = scope.get("category_scope")
		gtype_scope = scope.get("grievance_type_scope")
		prov_scope = scope.get("service_provider_scope")
		area_scope = scope.get("administrative_area_scope")

		include_parts = []

		if dept_scope:
			include_parts.append(f"`tabGrievance`.assigned_dept = {frappe.db.escape(dept_scope)}")
		if cat_scope:
			include_parts.append(f"`tabGrievance`.service_category = {frappe.db.escape(cat_scope)}")
		if gtype_scope:
			include_parts.append(f"`tabGrievance`.grievance_type = {frappe.db.escape(gtype_scope)}")
		if prov_scope:
			include_parts.append(
				f"`tabGrievance`.associated_service_provider = {frappe.db.escape(prov_scope)}"
			)
		if area_scope:
			area_lft, area_rgt = bounds.get(area_scope, (None, None))
			if area_lft is not None and area_rgt is not None:
				include_parts.append(
					f"(`tabGrievance`.area_lft >= {int(area_lft)} and `tabGrievance`.area_lft <= {int(area_rgt)})"
				)

		if include_parts:
			clauses.append("(`tabGrievance`.docstatus != 0 and " + " and ".join(include_parts) + ")")
	return clauses


def _scope_covers(doc, scopes) -> bool:
	"""Whether the case falls inside any of `scopes`. A scope with no field set covers nothing."""
	dept = doc.get("assigned_dept")
	category = doc.get("service_category")
	grievance_type = doc.get("grievance_type")
	provider = doc.get("associated_service_provider")
	area = doc.get("administrative_area")
	case_lft = doc.get("area_lft")
	if case_lft is None and area:
		case_lft = frappe.db.get_value("Grievance Administrative Area", area, "lft")

	for scope in scopes:
		dept_scope = scope.get("department_scope")
		cat_scope = scope.get("category_scope")
		gtype_scope = scope.get("grievance_type_scope")
		prov_scope = scope.get("service_provider_scope")
		area_scope = scope.get("administrative_area_scope")

		if not (dept_scope or cat_scope or gtype_scope or prov_scope or area_scope):
			continue

		if dept_scope and dept != dept_scope:
			continue
		if cat_scope and category != cat_scope:
			continue
		if gtype_scope and grievance_type != gtype_scope:
			continue
		if prov_scope and provider != prov_scope:
			continue
		if area_scope:
			if case_lft is None or not is_in_area_subtree(case_lft, area_scope):
				continue

		return True

	return False


def _is_placed(user):
	"""Whether `user` has been put on a desk at all, active or not."""
	return bool(frappe.db.exists("Grievance RBAC Assignment Officer", {"user": user}))


def _reviewer_clause(user):
	"""What a Review Officer reads: the cases in their desks' scopes, never an unsent draft.

	A reviewer placed on a department's desks reads that department's cases only, and loses
	access when every desk is retired. One who was never placed on a desk keeps the oversight
	of every filed case that Review Officers had before desks were assigned to them.
	"""
	if not _is_placed(user):
		return "`tabGrievance`.docstatus != 0"
	scoped = _scope_clauses(active_scopes(user))
	return "(" + " or ".join(scoped) + ")" if scoped else "1 = 0"


def grievance_query_conditions(user=None):
	"""SQL appended to every Grievance list query. Deny-by-default.

	- Submitters only see their own cases and assisted submissions.
	- Officers see cases assigned to themselves and cases assigned to subordinate officers in their reporting chain.
	- Admins see all cases.
	- Review Officers see the cases in the scopes of the desks they are placed on, and every
	  case that has left Draft if they were never placed on one.
	"""
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))

	if roles & UNRESTRICTED_ROLES:
		return ""

	clauses = []

	# Oversight reads every filed case but never someone's unsent draft.
	if ROLE_REVIEW_OFFICER in roles:
		clauses.append(_reviewer_clause(user))

	# A submitter reaches their own cases, and the assisted submissions they
	# filed on someone else's behalf. Both arms belong to the one Submitter role.
	if roles & SUBMITTER_ROLES:
		profiles = profiles_of(user)
		if profiles:
			clauses.append(f"`tabGrievance`.submitter in ({_quote(profiles)})")
		clauses.append(f"`tabGrievance`.assisted_by_officer = {frappe.db.escape(user)}")
		clauses.append(f"`tabGrievance`.owner = {frappe.db.escape(user)}")

	# Grievance Officer: sees cases assigned to self/subordinates and cases within configured scope
	if ROLE_OFFICER in roles:
		scope_clauses = _scope_clauses(active_scopes(user))
		team = get_subordinate_officers(user)
		scope_clauses.append(
			f"(`tabGrievance`.assigned_to in ({_quote(team)}) and `tabGrievance`.docstatus != 0)"
		)
		clauses.append("(" + " or ".join(scope_clauses) + ")")

	if not clauses:
		return "1 = 0"
	return "(" + " or ".join(clauses) + ")"


def has_grievance_permission(doc, ptype="read", user=None):
	"""Per-document check. Mirrors the list conditions for a single record."""
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))

	if roles & UNRESTRICTED_ROLES:
		return True

	if ROLE_REVIEW_OFFICER in roles and ptype == "read" and int(doc.get("docstatus") or 0) != 0:
		if not _is_placed(user) or _scope_covers(doc, active_scopes(user)):
			return True

	if roles & SUBMITTER_ROLES:
		owner = doc.get("owner")
		submitter = doc.get("submitter")
		assisted = doc.get("assisted_by_officer")
		docstatus = doc.get("docstatus")
		owns = (bool(submitter) and submitter in profiles_of(user)) or (bool(owner) and owner == user)
		if owns or assisted == user:
			if ptype == "read":
				return True
			return ptype == "write" and int(docstatus or 0) != 2

	if ROLE_OFFICER not in roles:
		return False

	if int(doc.get("docstatus") or 0) == 0:
		return False

	team = get_subordinate_officers(user)
	assigned = doc.get("assigned_to")
	if assigned and assigned in team:
		# Visibility granted for all cases in reporting chain; editing permitted for assignee or supervisor
		return True

	if ptype == "read" and frappe.db.exists(
		"Grievance Change Request",
		{"grievance": doc.get("name"), "status": "Pending", "pending_with": user},
	):
		return True

	# Scope grants visibility; editing still needs the case assigned.
	return ptype == "read" and _scope_covers(doc, active_scopes(user))


def _template_scope_clause(scope):
	"""SQL matching templates one RBAC scope may see: an empty template dimension
	matches everyone, an empty scope dimension matches every template."""
	parts = []
	for scope_field, template_field in (
		("department_scope", "department"),
		("category_scope", "service_category"),
	):
		value = scope.get(scope_field)
		if value:
			parts.append(
				f"ifnull(`tabGrievance Response Template`.{template_field}, '') in ('', {frappe.db.escape(value)})"
			)
	return "(" + " and ".join(parts) + ")" if parts else "1 = 1"


def response_template_query_conditions(user=None):
	"""Officers list the active templates within their department and category scope."""
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))
	if roles & UNRESTRICTED_ROLES:
		return ""
	if ROLE_OFFICER not in roles:
		return "1 = 0"

	scopes = active_scopes(user)
	clauses = [_template_scope_clause(scope) for scope in scopes] or [
		(
			"(ifnull(`tabGrievance Response Template`.department, '') = ''"
			+ " and ifnull(`tabGrievance Response Template`.service_category, '') = '')"
		)
	]
	return "(`tabGrievance Response Template`.is_active = 1 and (" + " or ".join(clauses) + "))"


def has_response_template_permission(doc, ptype="read", user=None):
	"""Per-document check. Mirrors the list conditions for a single template."""
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))
	if roles & UNRESTRICTED_ROLES:
		return True
	if ROLE_OFFICER not in roles or ptype != "read" or not doc.get("is_active"):
		return False

	def fits(scope):
		for scope_field, template_field in (
			("department_scope", "department"),
			("category_scope", "service_category"),
		):
			value = scope.get(scope_field)
			if value and doc.get(template_field) and doc.get(template_field) != value:
				return False
		return True

	scopes = active_scopes(user)
	if not scopes:
		return not doc.get("department") and not doc.get("service_category")
	return any(fits(scope) for scope in scopes)


def outranks(approver, assignee, if_unplaced=True):
	"""True when the approver sits strictly higher in the escalation chain.

	`if_unplaced` is the answer when the chain cannot place one of them. Pass False
	where only positive proof of seniority will do.
	"""
	approver_level = current_level_of(approver)
	assignee_level = current_level_of(assignee)
	if not approver_level or not assignee_level:
		return if_unplaced

	orders = {
		row.name: row.level_order
		for row in frappe.get_all(
			"Grievance Role Level", filters={"is_active": 1}, fields=["name", "level_order"]
		)
	}
	return orders.get(approver_level, 0) > orders.get(assignee_level, 0)


def head_scopes(user) -> list[tuple[str | None, str | None]]:
	"""(department, category) of each top-rung record `user` holds; None is every one."""
	from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
		query_active_officer_assignments,
		top_rung,
	)

	top = top_rung()
	if not top or ROLE_DESK_HEAD not in frappe.get_roles(user):
		return []
	rows = query_active_officer_assignments(
		user=user, role_level=top, fields=["p.department_scope", "p.category_scope"], order_by=None
	)
	return [(row.department_scope or None, row.category_scope or None) for row in rows]


def heads_category(user, category) -> bool:
	"""Whether `user` heads `category`: a record with no category covers the categories its
	department serves, or all of them with no department."""
	if is_unrestricted(user):
		return True
	for dept, cat in head_scopes(user):
		if cat == category:
			return True
		if cat is None and (
			dept is None
			or frappe.db.exists(
				"Grievance RBAC Assignment",
				{"department_scope": dept, "category_scope": category, "active": 1},
			)
		):
			return True
	return False


def assert_heads_category(user, category) -> None:
	if not heads_category(user, category):
		frappe.throw(_("You are not the desk head for {0}.").format(category), frappe.PermissionError)


# What a Desk Head's DocPerm rows on Grievance Type and Grievance RBAC Assignment grant, so
# the API's own saves pass. The hooks below narrow them to the head's records for any caller,
# so Frappe's generic /api/resource cannot reach past what the officer and taxonomy APIs allow.
WRITE_PTYPES = frozenset({"write", "create", "delete", "submit", "cancel", "amend"})


def _desk_head_restricted(ptype, user) -> bool:
	"""Whether `user` is a Desk Head, not an admin, asking for a write."""
	return ptype in WRITE_PTYPES and not is_unrestricted(user) and ROLE_DESK_HEAD in frappe.get_roles(user)


def has_rbac_assignment_permission(doc, ptype="read", user=None):
	"""A Desk Head writes only a record they head. Everyone else is left to the DocPerm rows.

	Frappe treats a falsy result as a denial, so "not my business" is True, not None.
	"""
	user = user or frappe.session.user
	if not _desk_head_restricted(ptype, user):
		return True
	if doc.is_new() or not doc.name:
		return False
	return doc.name in _head_desks(user)


def has_grievance_type_permission(doc, ptype="read", user=None):
	"""A Desk Head writes only types of categories they head, and cannot move one out of them."""
	user = user or frappe.session.user
	if not _desk_head_restricted(ptype, user):
		return True
	categories = {doc.get("service_category")}
	if not doc.is_new() and doc.name:
		categories.add(frappe.db.get_value("Grievance Type", doc.name, "service_category"))
	return all(category and heads_category(user, category) for category in categories)


def _head_desks(user) -> set[str]:
	"""The RBAC records on which `user` holds an active top-rung row (as `officer._head_records`)."""
	from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
		top_rung,
	)

	top = top_rung()
	if not top:
		return set()
	return set(
		frappe.get_all(
			"Grievance RBAC Assignment Officer",
			filters={
				"user": user,
				"role_level": top,
				"active": 1,
				"parenttype": "Grievance RBAC Assignment",
			},
			pluck="parent",
		)
	)


def is_department_head(user, department, area=None, category=None):
	"""Whether `user` holds the top active rung (department head) covering department, category and area."""
	from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
		holds_rung,
		top_rung,
	)

	top = top_rung()
	if not top:
		return False
	return holds_rung(user, top, department=department, area=area, category=category)
