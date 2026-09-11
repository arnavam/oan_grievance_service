# Copyright (c) 2026, COSS - Centre for Open Societal Systems and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from oan_grievance_service.setup.install import ROLE_LEVELS, seed_role_levels


class TestGrievanceRoleLevel(FrappeTestCase):
	def test_seed_creates_every_fsd_level(self):
		seed_role_levels()
		for code, _name, _order, _desc in ROLE_LEVELS:
			self.assertTrue(
				frappe.db.exists("Grievance Role Level", code),
				f"{code} was not seeded",
			)

	def test_seed_is_idempotent(self):
		seed_role_levels()
		before = frappe.db.count("Grievance Role Level")
		self.assertEqual(seed_role_levels(), [])
		self.assertEqual(frappe.db.count("Grievance Role Level"), before)

	def test_level_code_becomes_the_record_name(self):
		"""Link values must be the stable FSD token, not an autoincremented id."""
		seed_role_levels()
		doc = frappe.get_doc("Grievance Role Level", "nodal_officer")
		self.assertEqual(doc.name, doc.level_code)

	def test_chain_is_ordered_junior_to_senior(self):
		seed_role_levels()
		rows = frappe.get_all(
			"Grievance Role Level",
			filters={"is_active": 1},
			fields=["name", "level_order"],
			order_by="level_order asc",
		)
		orders = [r.level_order for r in rows]
		self.assertEqual(orders, sorted(orders))
		self.assertEqual(rows[0].name, "nodal_officer")
		self.assertEqual(rows[-1].name, "department_head")

	def test_duplicate_active_order_is_rejected(self):
		seed_role_levels()
		taken = frappe.db.get_value("Grievance Role Level", "senior_nodal_officer", "level_order")
		dupe = frappe.get_doc(
			{
				"doctype": "Grievance Role Level",
				"level_code": "test_clashing_level",
				"level_name": "Test Clashing Level",
				"level_order": taken,
				"is_active": 1,
			}
		)
		self.assertRaises(frappe.ValidationError, dupe.insert)

	def test_inactive_level_may_reuse_an_order(self):
		"""A retired level keeps its historic rank without blocking its replacement."""
		seed_role_levels()
		taken = frappe.db.get_value("Grievance Role Level", "senior_nodal_officer", "level_order")
		retired = frappe.get_doc(
			{
				"doctype": "Grievance Role Level",
				"level_code": "test_retired_level",
				"level_name": "Test Retired Level",
				"level_order": taken,
				"is_active": 0,
			}
		)
		retired.insert()
		self.assertTrue(frappe.db.exists("Grievance Role Level", "test_retired_level"))

	def test_rbac_assignment_links_to_role_level(self):
		"""Grievance RBAC Assignment must link to a valid Grievance Role Level."""
		from oan_grievance_service.permissions import find_officer_by_role_level

		seed_role_levels()
		test_user = "test_nodal_user@example.com"
		if not frappe.db.exists("User", test_user):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": test_user,
					"first_name": "Test",
					"last_name": "Nodal Officer",
				}
			).insert(ignore_permissions=True)

		assignment = frappe.get_doc(
			{
				"doctype": "Grievance RBAC Assignment",
				"user": test_user,
				"role_level": "nodal_officer",
				"is_primary": 1,
				"active": 1,
				"effective_from": frappe.utils.today(),
			}
		).insert(ignore_permissions=True)

		self.assertEqual(assignment.role_level, "nodal_officer")
		resolved = find_officer_by_role_level("nodal_officer")
		self.assertEqual(resolved, test_user)
