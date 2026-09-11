"""FR-01 / 3.1.1 Role-Based Access Control, deny-by-default.

The service runs on three capability roles and only three. A role answers *what actions
exist for you*; it never answers *which cases you may touch*. That second question is
answered by the Grievance RBAC Assignment records for the user - region, department and
category - applied as a permission query condition, so it filters list views, reports
and the API uniformly rather than being re-checked per screen.

Seniority is deliberately absent from the role list. The former L1 / L2 / Department
Head roles were rungs of a hierarchy, not distinct capabilities, and are replaced by
position in the reporting chain. See
.docs/sla_workflows_and_lifecycle_specification.md §10.1.

The FSD is explicit that routing eligibility does not by itself grant edit rights: an
explicit case assignment or approval permission is required. That distinction is what
this module enforces.
"""

import frappe

from oan_grievance_service.services import constants as C

ROLE_SUBMITTER = "Grievance Submitter"
ROLE_OFFICER = "Grievance Officer"
ROLE_ADMIN = "Grievance Admin"

GRIEVANCE_ROLES = (ROLE_ADMIN, ROLE_OFFICER, ROLE_SUBMITTER)

# FSD Appendix F: administrators see all regions, departments and categories.
UNRESTRICTED_ROLES = {ROLE_ADMIN, "System Manager", "Administrator"}

# The only states in which a case is actually waiting on its submitter. Outside these,
# a submitter reads their case but cannot alter it.
SUBMITTER_WRITABLE_STATUSES = frozenset({C.MORE_INFO_NEEDED, C.PENDING_SUBMITTER})


def active_scopes(user=None):
	"""The user's live RBAC assignments, honouring the effective date window."""
	user = user or frappe.session.user
	today = frappe.utils.today()
	return frappe.get_all(
		"Grievance RBAC Assignment",
		filters={
			"user": user,
			"active": 1,
			"effective_from": ["<=", today],
		},
		or_filters=[
			["effective_to", "is", "not set"],
			["effective_to", ">=", today],
		],
		fields=[
			"administrative_area_scope",
			"department_scope",
			"category_scope",
			"role_level",
			"is_primary",
		],
	)


def find_officer_by_role_level(role_level, department=None, administrative_area=None):
	"""Dynamically resolve an officer user from active Grievance RBAC Assignments.

	Honours role_level, geographic jurisdiction (area tree interval), line department
	(NULL for nodal officers who cover all departments in an area), and primary post priority.
	"""
	today = frappe.utils.today()
	assignments = frappe.get_all(
		"Grievance RBAC Assignment",
		filters={
			"active": 1,
			"role_level": role_level,
			"effective_from": ["<=", today],
		},
		or_filters=[
			["effective_to", "is", "not set"],
			["effective_to", ">=", today],
		],
		fields=["user", "administrative_area_scope", "department_scope", "is_primary"],
		order_by="is_primary desc, modified desc",
	)
	if not assignments:
		return None

	target_lft = None
	if administrative_area:
		target_lft = frappe.db.get_value("Administrative Area", administrative_area, "lft")

	for a in assignments:
		# Department filter: if assignment specifies a department, it must match.
		if department and a.department_scope and a.department_scope != department:
			continue
		# Area filter: if assignment specifies an area, target area must be in its subtree.
		if target_lft is not None and a.administrative_area_scope:
			area_info = frappe.db.get_value(
				"Administrative Area",
				a.administrative_area_scope,
				["lft", "rgt"],
				as_dict=True,
			)
			if area_info and area_info.lft is not None and area_info.rgt is not None:
				if not (area_info.lft <= int(target_lft) <= area_info.rgt):
					continue
		return a.user

	return None


def area_bounds(scopes):
	"""Nested Set intervals for every area named by `scopes`, in one query.

	Resolved live rather than denormalised onto the assignment row. Frappe's NestedSet
	shifts `lft`/`rgt` across the tree whenever a node is inserted or moved, so a stamped
	copy silently drifts out of the coordinate system it is compared against - and on a
	scope row that drift widens or narrows what an officer can see.
	"""
	names = {s.administrative_area_scope for s in scopes if s.administrative_area_scope}
	if not names:
		return {}
	return {
		a.name: (a.lft, a.rgt)
		for a in frappe.get_all(
			"Administrative Area",
			filters={"name": ["in", list(names)]},
			fields=["name", "lft", "rgt"],
		)
		if a.lft is not None and a.rgt is not None
	}


def _quote(values):
	return ", ".join(frappe.db.escape(v) for v in values if v)


def _submitter_profiles(user):
	"""Profiles this user owns.

	Resolved through the explicit `user` link rather than by matching a contact address,
	so changing a contact email cannot transfer someone else's cases, and two profiles
	sharing an address do not both match.
	"""
	return frappe.get_all("Submitter Profile", filters={"user": user}, pluck="name")


def grievance_query_conditions(user=None):
	"""SQL appended to every Grievance list query. Deny-by-default.

	Uses O(1) Nested Set tree interval containment (`area_lft BETWEEN scope_lft AND scope_rgt`).
	"""
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))

	if roles & UNRESTRICTED_ROLES:
		return ""

	clauses = []

	# FSD 3.1.1: a submitter reaches their own cases, and the assisted submissions they
	# filed on someone else's behalf. Both arms belong to the one Submitter role.
	if ROLE_SUBMITTER in roles:
		profiles = _submitter_profiles(user)
		if profiles:
			clauses.append(f"`tabGrievance`.submitter in ({_quote(profiles)})")
		clauses.append(f"`tabGrievance`.assisted_by_officer = {frappe.db.escape(user)}")

	# FSD 3.1.1: officers act on assigned cases within their configured scope.
	if ROLE_OFFICER in roles:
		scope_clauses = []
		scopes = active_scopes(user)
		bounds = area_bounds(scopes)
		for scope in scopes:
			parts = []
			if scope.department_scope:
				parts.append(f"`tabGrievance`.assigned_dept = {frappe.db.escape(scope.department_scope)}")
			if scope.category_scope:
				parts.append(f"`tabGrievance`.service_category = {frappe.db.escape(scope.category_scope)}")
			if scope.administrative_area_scope:
				area_lft, area_rgt = bounds.get(scope.administrative_area_scope, (None, None))
				if area_lft is not None and area_rgt is not None:
					parts.append(
						f"(`tabGrievance`.area_lft >= {int(area_lft)} and `tabGrievance`.area_lft <= {int(area_rgt)})"
					)

			if parts:
				scope_clauses.append("(" + " and ".join(parts) + ")")
			else:
				scope_clauses.append("1 = 1")

		# An assigned case is always visible to its own officer.
		scope_clauses.append(f"`tabGrievance`.assigned_to = {frappe.db.escape(user)}")
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

	if ROLE_SUBMITTER in roles:
		owns = bool(doc.submitter) and doc.submitter in _submitter_profiles(user)
		if owns or doc.assisted_by_officer == user:
			if ptype == "read":
				return True
			return ptype == "write" and doc.status in SUBMITTER_WRITABLE_STATUSES

	if ROLE_OFFICER not in roles:
		return False

	if doc.assigned_to == user:
		return True

	case_lft = getattr(doc, "area_lft", None)
	if case_lft is None and getattr(doc, "administrative_area", None):
		case_lft = frappe.db.get_value("Administrative Area", doc.administrative_area, "lft")

	scopes = active_scopes(user)
	bounds = area_bounds(scopes)
	for scope in scopes:
		if scope.department_scope and doc.assigned_dept != scope.department_scope:
			continue
		if scope.category_scope and doc.service_category != scope.category_scope:
			continue
		if scope.administrative_area_scope:
			scope_lft, scope_rgt = bounds.get(scope.administrative_area_scope, (None, None))
			if scope_lft is not None and scope_rgt is not None:
				if case_lft is None or not (scope_lft <= int(case_lft) <= scope_rgt):
					continue

		# FSD 3.1.1: scope grants visibility; editing still needs the case assigned.
		return True if ptype == "read" else doc.assigned_to == user

	return False


def can_approve_reassignment(user=None):
	"""FSD 3.3.1: a reassignment is decided by a supervisor, not by its requester.

	TODO(spec §10.5): the correct test is that the approver is a common ancestor of the
	current and target assignees in the reporting chain. Until chain.py lands this is
	role-only, which is broader than intended - any officer may approve.
	"""
	roles = set(frappe.get_roles(user or frappe.session.user))
	return bool(roles & ({ROLE_OFFICER} | UNRESTRICTED_ROLES))


def can_approve_deferral(user=None):
	"""FSD 3.11.7: supervisor approval unless policy explicitly permits self-approval.

	TODO(spec §10.5): same as above - should be `is_ancestor(approver, assignee)`.
	"""
	roles = set(frappe.get_roles(user or frappe.session.user))
	return bool(roles & ({ROLE_OFFICER} | UNRESTRICTED_ROLES))
