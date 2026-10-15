# Copyright (c) 2026, COSS - Centre for Open Societal Systems and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
	active_scopes,
	current_level_of,
	find_officer_by_role_level,
	get_officer_supervisor,
)
from oan_grievance_service.permissions import (
	grievance_query_conditions,
	has_grievance_permission,
)
from oan_grievance_service.services import constants as C
from oan_grievance_service.services import routing
from oan_grievance_service.setup.install import seed_unrouted_desk
from oan_grievance_service.tests.fixtures import a_leaf_area, discard_grievance


def _set_unrouted_desk_active(active: int) -> None:
	"""Switch the seeded catch-all desk on or off; it takes any case no other desk can."""
	seed_unrouted_desk()
	frappe.db.set_value(
		"Grievance RBAC Assignment", {"department_scope": C.UNROUTED_DEPARTMENT}, "active", active
	)


class TestGrievanceRBACAssignment(FrappeTestCase):
	def setUp(self):
		# Create test users
		self.users = {}
		for name, email in [
			("Primary Officer", "prim_officer@example.com"),
			("Secondary Officer", "sec_officer@example.com"),
			("Least Loaded Officer", "least_officer@example.com"),
		]:
			if not frappe.db.exists("User", email):
				doc = frappe.get_doc(
					{
						"doctype": "User",
						"email": email,
						"first_name": name,
						"roles": [{"role": "Grievance Officer"}],
					}
				).insert(ignore_permissions=True)
			else:
				doc = frappe.get_doc("User", email)
			self.users[email] = doc

		# Ensure department & category
		if not frappe.db.exists("Grievance Department", "Unified Agri Dept"):
			frappe.get_doc(
				{
					"doctype": "Grievance Department",
					"dept_name": "Unified Agri Dept",
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Grievance Service Category", "Inputs"):
			frappe.get_doc(
				{
					"doctype": "Grievance Service Category",
					"category_name": "Inputs",
					"code": "001",
					"is_active": 1,
				}
			).insert(ignore_permissions=True)

		# Tests in this class share one transaction, and a category desk is unique per
		# department, so each test starts without the previous test's desks.
		for name in frappe.get_all(
			"Grievance RBAC Assignment", filters={"department_scope": "Unified Agri Dept"}, pluck="name"
		):
			frappe.delete_doc("Grievance RBAC Assignment", name, force=True, ignore_permissions=True)

		# Ensure role level
		if not frappe.db.exists("Grievance Role Level", "nodal_officer"):
			frappe.get_doc(
				{
					"doctype": "Grievance Role Level",
					"level_code": "nodal_officer",
					"level_name": "Nodal Officer",
					"hierarchy_order": 10,
					"is_active": 1,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Grievance Submitter Type", "Individual Farmer"):
			frappe.get_doc(
				{
					"doctype": "Grievance Submitter Type",
					"type_name": "Individual Farmer",
					"code": "IND",
					"is_active": 1,
				}
			).insert(ignore_permissions=True)

		self.gtype_name = frappe.db.get_value("Grievance Type", {"type_name": "Seed Quality Issue"}, "name")
		if not self.gtype_name:
			gtype = frappe.get_doc(
				{
					"doctype": "Grievance Type",
					"type_name": "Seed Quality Issue",
					"service_category": "Inputs",
					"is_active": 1,
				}
			).insert(ignore_permissions=True)
			self.gtype_name = gtype.name

		# The woreda needs a region above it: the ticket number takes its first
		# character from the region, so an orphan woreda cannot be filed against.
		if not frappe.db.exists("Grievance Administrative Area", "TLR"):
			frappe.get_doc(
				{
					"doctype": "Grievance Administrative Area",
					"area_name": "Test Leaf Region",
					"level_name": "Region",
					"code": "TLR",
					"ticket_code": "R",
					"is_group": 1,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Grievance Administrative Area", "TLR.TLW"):
			frappe.get_doc(
				{
					"doctype": "Grievance Administrative Area",
					"area_name": "Test Leaf Woreda",
					"level_name": "Woreda",
					"code": "TLW",
					"parent_administrative_area": "TLR",
					"is_group": 0,
				}
			).insert(ignore_permissions=True)

	def test_active_scopes_and_permissions(self):
		"""Verify active_scopes and has_grievance_permission query the unified desk record."""
		desk = frappe.get_doc(
			{
				"doctype": "Grievance RBAC Assignment",
				"department_scope": "Unified Agri Dept",
				"category_scope": "Inputs",
				"active": 1,
				"effective_from": frappe.utils.today(),
				"officers": [
					{
						"user": "prim_officer@example.com",
						"role_level": "nodal_officer",
						"is_primary": 1,
						"active": 1,
					}
				],
			}
		).insert(ignore_permissions=True)

		scopes = active_scopes("prim_officer@example.com")
		self.assertIsNotNone(desk.name)
		self.assertTrue(any(s.get("department_scope") == "Unified Agri Dept" for s in scopes))

		# Check permission on a ticket matching department and category
		fake_grievance = frappe._dict(
			{
				"assigned_dept": "Unified Agri Dept",
				"service_category": "Inputs",
				"assigned_to": "other_officer@example.com",
				"status": "In Progress",
				"docstatus": 1,
			}
		)
		self.assertTrue(
			has_grievance_permission(fake_grievance, ptype="read", user="prim_officer@example.com")
		)

	def test_two_tier_routing_strategies(self):
		"""Verify Tier 2 officer resolution under Primary First, Round Robin, and Least Loaded."""
		desk = frappe.get_doc(
			{
				"doctype": "Grievance RBAC Assignment",
				"department_scope": "Unified Agri Dept",
				"category_scope": "Inputs",
				"routing_strategy": "Round Robin",
				"active": 1,
				"effective_from": frappe.utils.today(),
				"officers": [
					{
						"user": "prim_officer@example.com",
						"role_level": "nodal_officer",
						"is_primary": 1,
						"active": 1,
					},
					{
						"user": "sec_officer@example.com",
						"role_level": "nodal_officer",
						"is_primary": 0,
						"active": 1,
					},
				],
			}
		).insert(ignore_permissions=True)

		# First round robin picks first (both have NULL last_assigned_at)
		picked1 = routing.pick_officer_by_strategy(desk)
		self.assertIn(picked1, ["prim_officer@example.com", "sec_officer@example.com"])

		# Second round robin picks the other officer
		desk.reload()
		picked2 = routing.pick_officer_by_strategy(desk)
		self.assertNotEqual(picked1, picked2)

		# Test Least Loaded
		desk.routing_strategy = "Least Loaded"
		desk.save(ignore_permissions=True)
		picked_ll = routing.pick_officer_by_strategy(desk)
		self.assertIsNotNone(picked_ll)

	def test_officer_reports_to_and_escalation_target(self):
		"""Verify reports_to on officer child table resolves supervisor and reassigns on escalation."""
		from oan_grievance_service.services import sla

		frappe.get_doc(
			{
				"doctype": "Grievance RBAC Assignment",
				"department_scope": "Unified Agri Dept",
				"category_scope": "Inputs",
				"active": 1,
				"effective_from": frappe.utils.today(),
				"officers": [
					{
						"user": "prim_officer@example.com",
						"role_level": "nodal_officer",
						"reports_to": "sec_officer@example.com",
						"is_primary": 1,
						"active": 1,
					},
					{
						"user": "sec_officer@example.com",
						"role_level": "senior_nodal_officer",
						"is_primary": 0,
						"active": 1,
					},
				],
			}
		).insert(ignore_permissions=True)

		supervisor = get_officer_supervisor("prim_officer@example.com", department="Unified Agri Dept")
		self.assertEqual(supervisor, "sec_officer@example.com")

		# Create fake grievance and escalate. The ticket number is not passed in:
		# autoname() generates and overwrites it, so a literal here only goes stale.
		fake_g = frappe.get_doc(
			{
				"doctype": "Grievance",
				"submission_channel": "Mobile App",
				"submitter_type": "Individual Farmer",
				"submitter_name": "Test Farmer",
				"contact_mobile": "+251911223344",
				"administrative_area": "TLR.TLW",
				"service_category": "Inputs",
				"grievance_type": self.gtype_name,
				"description": "Seed germination failed across the plot",
				"assigned_dept": "Unified Agri Dept",
				"assigned_to": "prim_officer@example.com",
				"status": "Assigned",
			}
		).insert(ignore_permissions=True)

		# One rung up: the case is handed to the nodal officer's own reports_to, and the
		# level it now sits at is that person's, not a counter on the grievance.
		res = sla.escalate(fake_g, reassign=True)
		self.assertEqual(res, "sec_officer@example.com")
		fake_g.reload()
		self.assertEqual(fake_g.escalated, 1)
		self.assertEqual(fake_g.assigned_to, "sec_officer@example.com")
		self.assertEqual(current_level_of(fake_g.assigned_to), "senior_nodal_officer")

	def test_routing_with_grievance_type_and_provider_scope(self):
		"""Verify routing prioritizes specific type and provider scopes over broad category rules."""
		broad_doc = frappe.get_doc(
			{
				"doctype": "Grievance RBAC Assignment",
				"department_scope": "Unified Agri Dept",
				"category_scope": "Inputs",
				"active": 1,
				"effective_from": frappe.utils.today(),
				"officers": [
					{
						"user": "prim_officer@example.com",
						"role_level": "nodal_officer",
						"is_primary": 1,
						"active": 1,
					}
				],
			}
		).insert(ignore_permissions=True)

		specific_doc = frappe.get_doc(
			{
				"doctype": "Grievance RBAC Assignment",
				"department_scope": "Unified Agri Dept",
				"category_scope": "Inputs",
				"grievance_type_scope": self.gtype_name,
				"service_provider_scope": "EthioSeed Corp",
				"active": 1,
				"effective_from": frappe.utils.today(),
				"officers": [
					{
						"user": "sec_officer@example.com",
						"role_level": "nodal_officer",
						"is_primary": 1,
						"active": 1,
					}
				],
			}
		).insert(ignore_permissions=True)

		try:
			case_data = {
				"service_category": "Inputs",
				"grievance_type": self.gtype_name,
				"associated_service_provider": "EthioSeed Corp",
				"administrative_area": "TLR.TLW",
			}
			matched = routing.find_matching_assignment(case_data)
			self.assertIsNotNone(matched)
			self.assertEqual(matched.name, specific_doc.name)
		finally:
			if frappe.db.exists("Grievance RBAC Assignment", broad_doc.name):
				frappe.delete_doc(
					"Grievance RBAC Assignment", broad_doc.name, force=True, ignore_permissions=True
				)
			if frappe.db.exists("Grievance RBAC Assignment", specific_doc.name):
				frappe.delete_doc(
					"Grievance RBAC Assignment", specific_doc.name, force=True, ignore_permissions=True
				)

	def test_route_grievance_job_worker(self):
		"""Test background worker route_grievance_job processes unassigned submitted cases."""
		desk = frappe.get_doc(
			{
				"doctype": "Grievance RBAC Assignment",
				"department_scope": "Unified Agri Dept",
				"category_scope": "Inputs",
				"active": 1,
				"effective_from": frappe.utils.today(),
				"officers": [
					{
						"user": "prim_officer@example.com",
						"role_level": "nodal_officer",
						"is_primary": 1,
						"active": 1,
					}
				],
			}
		).insert(ignore_permissions=True)

		fake_g = frappe.get_doc(
			{
				"doctype": "Grievance",
				"submission_channel": "Mobile App",
				"submitter_type": "Individual Farmer",
				"submitter_name": "Queue Submitter",
				"contact_mobile": "+251911998877",
				"administrative_area": "TLR.TLW",
				"service_category": "Inputs",
				"grievance_type": self.gtype_name,
				"description": "Fertilizer delivery delayed in the region for multiple weeks.",
				"workflow_state": "Submitted",
				"status": "Submitted",
				"docstatus": 1,
			}
		).insert(ignore_permissions=True)

		try:
			routing.route_grievance_job(fake_g.name)
			fake_g.reload()
			self.assertEqual(fake_g.workflow_state, "Assigned")
			self.assertEqual(fake_g.assigned_dept, "Unified Agri Dept")
			self.assertEqual(fake_g.assigned_to, "prim_officer@example.com")
			self.assertEqual(fake_g.routed_automatically, 1)
			self.assertEqual(fake_g.routing_rule, desk.name)
		finally:
			if frappe.db.exists("Grievance", fake_g.name):
				discard_grievance(fake_g.name)
			if frappe.db.exists("Grievance RBAC Assignment", desk.name):
				frappe.delete_doc("Grievance RBAC Assignment", desk.name, force=True, ignore_permissions=True)

	def test_the_unrouted_desk_is_the_last_resort(self):
		_set_unrouted_desk_active(1)
		case = {"service_category": "Credit", "grievance_type": None, "associated_service_provider": None}
		departments = [desk.department_scope for desk in routing.ranked_assignments(case)]
		self.assertEqual(departments[-1:], [C.UNROUTED_DEPARTMENT])
		self.assertEqual(departments.count(C.UNROUTED_DEPARTMENT), 1)

		_set_unrouted_desk_active(0)
		departments = [desk.department_scope for desk in routing.ranked_assignments(case)]
		self.assertNotIn(C.UNROUTED_DEPARTMENT, departments)

	def test_a_desk_set_to_another_provider_or_type_never_takes_the_case(self):
		"""Only desks whose set scopes equal the case's rank; the rest fall through to Unrouted."""
		_set_unrouted_desk_active(1)

		def desk(**scopes):
			return frappe.get_doc(
				{
					"doctype": "Grievance RBAC Assignment",
					"department_scope": "Unified Agri Dept",
					"category_scope": "Inputs",
					"active": 1,
					"effective_from": frappe.utils.today(),
					**scopes,
				}
			).insert(ignore_permissions=True)

		provider_desk = desk(service_provider_scope="EthioSeed Corp").name
		type_desk = desk(grievance_type_scope=self.gtype_name, service_provider_scope="EthioSeed Corp").name

		def ranked(**case):
			case = {"service_category": "Inputs", "grievance_type": None, **case}
			return [desk.name for desk in routing.ranked_assignments(case)]

		self.assertIn(provider_desk, ranked(associated_service_provider="EthioSeed Corp"))
		self.assertNotIn(provider_desk, ranked(associated_service_provider="Another Corp"))
		self.assertNotIn(provider_desk, ranked(associated_service_provider=None))
		# A desk set to a type and a provider needs both.
		self.assertNotIn(
			type_desk, ranked(associated_service_provider="EthioSeed Corp", grievance_type="Other Type")
		)
		self.assertIn(
			type_desk, ranked(associated_service_provider="EthioSeed Corp", grievance_type=self.gtype_name)
		)
		# Whatever else is on the list, the unrouted desk is last.
		self.assertEqual(
			frappe.db.get_value("Grievance RBAC Assignment", ranked()[-1], "department_scope"),
			C.UNROUTED_DEPARTMENT,
		)

	def test_drain_routing_queue_preserves_unrouted_submitted_cases(self):
		"""Test drain_routing_queue routes matching cases and leaves non-matching cases in manual queue without deletion.

		A case that no desk serves goes to the Unrouted desk while that desk is active, so it is
		switched off here: the queue is what keeps the case then.
		"""
		_set_unrouted_desk_active(0)
		desk = frappe.get_doc(
			{
				"doctype": "Grievance RBAC Assignment",
				"department_scope": "Unified Agri Dept",
				"category_scope": "Inputs",
				"service_provider_scope": "EthioSeed Corp",
				"active": 1,
				"effective_from": frappe.utils.today(),
				"officers": [
					{
						"user": "prim_officer@example.com",
						"role_level": "nodal_officer",
						"is_primary": 1,
						"active": 1,
					}
				],
			}
		).insert(ignore_permissions=True)

		# Case 1: Matches Inputs rule with EthioSeed Corp provider
		matching_g = frappe.get_doc(
			{
				"doctype": "Grievance",
				"submission_channel": "Mobile App",
				"submitter_type": "Individual Farmer",
				"submitter_name": "Matching Submitter",
				"contact_mobile": "+251911998877",
				"administrative_area": "TLR.TLW",
				"service_category": "Inputs",
				"grievance_type": self.gtype_name,
				"associated_service_provider": "EthioSeed Corp",
				"description": "Seeds quality issue affecting the harvest yield significantly.",
				"workflow_state": "Submitted",
				"status": "Submitted",
				"docstatus": 1,
			}
		).insert(ignore_permissions=True)

		# Case 2: No rule matches (Credit category has no desk rules configured)
		gtype_credit = frappe.db.get_value("Grievance Type", {"service_category": "Credit"}, "name")
		unmatched_g = frappe.get_doc(
			{
				"doctype": "Grievance",
				"submission_channel": "Mobile App",
				"submitter_type": "Individual Farmer",
				"submitter_name": "Unmatched Submitter",
				"contact_mobile": "+251911998877",
				"administrative_area": "TLR.TLW",
				"service_category": "Credit",
				"grievance_type": gtype_credit,
				"description": "Credit loan subsidy query with no active rule configured in system.",
				"workflow_state": "Submitted",
				"status": "Submitted",
				"docstatus": 1,
			}
		).insert(ignore_permissions=True)

		try:
			from oan_grievance_service.tasks import drain_routing_queue

			routed_count = drain_routing_queue()
			self.assertGreaterEqual(routed_count, 1)

			matching_g.reload()
			self.assertEqual(matching_g.workflow_state, "Assigned")
			self.assertEqual(matching_g.assigned_to, "prim_officer@example.com")

			unmatched_g.reload()
			# Case must NOT be deleted, and must stay in Submitted state for manual nodal triage
			self.assertTrue(frappe.db.exists("Grievance", unmatched_g.name))
			self.assertEqual(unmatched_g.workflow_state, "Submitted")
			self.assertIsNone(unmatched_g.assigned_to)
			self.assertEqual(unmatched_g.routed_automatically, 0)
		finally:
			if frappe.db.exists("Grievance RBAC Assignment", desk.name):
				frappe.delete_doc("Grievance RBAC Assignment", desk.name, force=True, ignore_permissions=True)

	def test_empty_officer_scope_matches_no_cases(self):
		"""An officer with empty scope assignment must not match all grievances."""
		officer_email = f"empty.scope.{frappe.generate_hash(length=8)}@example.com"
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": officer_email,
				"first_name": "EmptyScope",
				"roles": [{"role": "Grievance Officer"}],
			}
		).insert(ignore_permissions=True)

		parent_assignment = frappe.get_doc(
			{
				"doctype": "Grievance RBAC Assignment",
				"assignment_name": f"Empty Scope {frappe.generate_hash(length=6)}",
				"active": 1,
				"effective_from": frappe.utils.today(),
				"department_scope": None,
				"category_scope": None,
				"officers": [
					{
						"user": user.name,
						"role_level": "nodal_officer",
						"is_primary": 1,
						"active": 1,
					}
				],
			}
		)
		parent_assignment.flags.ignore_mandatory = True
		parent_assignment.insert(ignore_permissions=True)

		cond = grievance_query_conditions(user.name)
		self.assertNotIn(
			"docstatus != 0)",
			cond.replace(
				f"`tabGrievance`.assigned_to in ('{user.name}') and `tabGrievance`.docstatus != 0",
				"",
			),
		)

		other_case = frappe.get_doc(
			{
				"doctype": "Grievance",
				"submission_channel": "Web Portal",
				"submitter_type": "Individual Farmer",
				"administrative_area": a_leaf_area(),
				"service_category": "Inputs",
				"grievance_type": self.gtype_name,
				"description": "Unassigned case for permission test",
				"assigned_to": "Administrator",
				"workflow_state": "In Progress",
				"status": "In Progress",
				"docstatus": 1,
			}
		)
		other_case.flags.ignore_mandatory = True
		other_case.insert(ignore_permissions=True)
		self.assertFalse(has_grievance_permission(other_case, ptype="read", user=user.name))
		discard_grievance(other_case.name)
