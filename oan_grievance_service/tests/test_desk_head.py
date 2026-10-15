# Copyright (c) 2026, COSS - Centre for Open Societal Systems and Contributors
# See license.txt

"""The Grievance Desk Head: a top-rung officer who manages only the people on their own records.

The role is a gate and the RBAC records are the scope (docs/desk-scoping-and-authority.md §5).
"""

import frappe

from oan_grievance_service import permissions
from oan_grievance_service.api.v1.officer import create_officer, update_officer
from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
	department_head_of,
)
from oan_grievance_service.services import constants as C
from oan_grievance_service.tests import test_officer_roles as base
from oan_grievance_service.tests.test_category_assignment import _keep_transaction


class TestDeskHead(base.RoleCase):
	def _desk_head(self, *categories):
		"""A head of `categories` in the first department, made as an admin would."""
		return self._head(service_categories=list(categories) or [self.inputs])

	def _new_officer_as(self, caller, email, **extra):
		frappe.set_user(caller)
		return create_officer(
			**{
				"full_name": "Marta Bekele",
				"level": "L1",
				"department": "S443",
				"email": email,
				"service_categories": [self.inputs],
				"temporary_password": base.PASSWORD,
				**extra,
			}
		)

	def test_the_role_follows_the_top_rung(self):
		officer = self._officer()
		self.assertNotIn(C.ROLE_DESK_HEAD, self._roles(officer["name"]))
		update_officer(officer["name"], level="L3")
		self.assertIn(C.ROLE_DESK_HEAD, self._roles(officer["name"]))
		update_officer(officer["name"], level="L1")
		self.assertNotIn(C.ROLE_DESK_HEAD, self._roles(officer["name"]))

	def test_a_new_head_gets_the_role(self):
		self.assertIn(C.ROLE_DESK_HEAD, self._roles(self._desk_head()["name"]))

	def test_a_head_adds_an_officer_to_their_own_records_reporting_to_them(self):
		head = self._desk_head()
		result = self._new_officer_as(head["name"], f"{base.PREFIX}added@example.com")
		self.assertEqual(result["status"], "success", msg=result)
		frappe.set_user("Administrator")
		rows = self._rows(f"{base.PREFIX}added@example.com")
		self.assertEqual({row.parent for row in rows}, {row.parent for row in self._rows(head["name"])})
		self.assertEqual({row.reports_to for row in rows}, {head["name"]})

	def test_a_head_cannot_place_an_officer_outside_their_records(self):
		head = self._desk_head(self.inputs)
		with _keep_transaction():
			result = self._new_officer_as(
				head["name"], f"{base.PREFIX}outside@example.com", service_categories=[self.credit]
			)
		self.assertEqual(result["code"], "PERMISSION_DENIED", msg=result)
		frappe.set_user("Administrator")
		self.assertFalse(frappe.db.exists("User", f"{base.PREFIX}outside@example.com"))

	def test_a_head_cannot_create_a_head_a_reviewer_or_a_department(self):
		head = self._desk_head()
		for extra in ({"level": "L3"}, {"role": "Reviewer", "level": None}, {"create_department": True}):
			with self.subTest(extra=extra), _keep_transaction():
				result = self._new_officer_as(head["name"], f"{base.PREFIX}nope@example.com", **extra)
				self.assertEqual(result["code"], "PERMISSION_DENIED", msg=result)
		frappe.set_user("Administrator")
		self.assertFalse(frappe.db.exists("User", f"{base.PREFIX}nope@example.com"))

	def test_a_head_edits_only_officers_on_their_own_records(self):
		head = self._desk_head(self.inputs)
		mine = self._officer(f"{base.PREFIX}mine@example.com")
		theirs = self._officer(f"{base.PREFIX}theirs@example.com", service_categories=[self.credit])
		frappe.set_user(head["name"])
		self.assertEqual(update_officer(mine["name"], full_name="Renamed")["status"], "success")
		with _keep_transaction():
			result = update_officer(theirs["name"], full_name="Renamed")
		self.assertEqual(result["code"], "PERMISSION_DENIED", msg=result)

	def test_a_head_cannot_edit_a_head_or_promote_an_officer(self):
		head = self._desk_head()
		other_head = self._head(f"{base.PREFIX}head2@example.com")
		mine = self._officer(f"{base.PREFIX}mine@example.com")
		frappe.set_user(head["name"])
		with _keep_transaction():
			for result in (
				update_officer(other_head["name"], full_name="Renamed"),
				update_officer(mine["name"], level="L3"),
				update_officer(mine["name"], department="S444"),
			):
				self.assertEqual(result["code"], "PERMISSION_DENIED", msg=result)

	def test_a_head_never_manages_an_account_that_holds_an_admin_role(self):
		head = self._desk_head()
		admin_officer = self._officer(f"{base.PREFIX}admin-officer@example.com")
		frappe.get_doc("User", admin_officer["name"]).add_roles(C.ROLE_ADMIN)
		admin_login = self._caller("admin-login", C.ROLE_ADMIN)
		frappe.set_user(head["name"])
		with _keep_transaction():
			self.assertEqual(
				update_officer(admin_officer["name"], full_name="Renamed")["code"], "PERMISSION_DENIED"
			)
			result = self._new_officer_as(head["name"], admin_login)
			self.assertEqual(result["code"], "PERMISSION_DENIED", msg=result)
		self.assertNotIn(C.ROLE_OFFICER, self._roles(admin_login))

	def _desk_of(self, category):
		return frappe.db.get_value(
			base.DESK, {"category_scope": category, "department_scope": self.department}
		)

	def _type(self, category):
		return frappe.get_doc(
			{
				"doctype": "Grievance Type",
				"service_category": category,
				"type_name": f"{base.PREFIX}type-{frappe.generate_hash(length=6)}",
				"is_active": 1,
			}
		).insert(ignore_permissions=True)

	def test_a_head_writes_only_the_rbac_records_they_head(self):
		"""The DocPerm row is narrowed for the generic resource API too, not only for ours."""
		head = self._desk_head(self.inputs)["name"]
		own, other = self._desk_of(self.inputs), self._desk_of(self.credit)
		self.assertTrue(frappe.has_permission(base.DESK, "write", doc=own, user=head))
		self.assertFalse(frappe.has_permission(base.DESK, "write", doc=other, user=head))
		self.assertFalse(frappe.has_permission(base.DESK, "create", doc=frappe.new_doc(base.DESK), user=head))
		# Reading is unchanged.
		self.assertTrue(frappe.has_permission(base.DESK, "read", doc=other, user=head))

	def test_other_roles_keep_what_their_docperm_rows_give(self):
		admin = self._caller("rbac-admin", C.ROLE_ADMIN)
		officer = self._officer()["name"]
		other = self._desk_of(self.credit)
		self.assertTrue(frappe.has_permission(base.DESK, "write", doc=other, user=admin))
		self.assertFalse(frappe.has_permission(base.DESK, "write", doc=other, user=officer))
		self.assertTrue(frappe.has_permission(base.DESK, "read", doc=other, user=officer))

	def test_a_head_writes_only_the_types_of_categories_they_head(self):
		head = self._desk_head(self.inputs)["name"]
		own, other = self._type(self.inputs), self._type(self.credit)
		self.assertTrue(frappe.has_permission("Grievance Type", "write", doc=own, user=head))
		self.assertFalse(frappe.has_permission("Grievance Type", "write", doc=other, user=head))
		for category, allowed in ((self.inputs, True), (self.credit, False)):
			new = frappe.new_doc("Grievance Type")
			new.update({"service_category": category, "type_name": "New type"})
			self.assertEqual(frappe.has_permission("Grievance Type", "create", doc=new, user=head), allowed)
		# Moving a type into or out of a category they do not head is refused either way.
		own.service_category = self.credit
		self.assertFalse(frappe.has_permission("Grievance Type", "write", doc=own, user=head))
		other.service_category = self.inputs
		self.assertFalse(frappe.has_permission("Grievance Type", "write", doc=other, user=head))
		# Reading types stays open.
		self.assertTrue(frappe.has_permission("Grievance Type", "read", doc=other, user=head))

	def test_a_head_heads_the_categories_of_their_records(self):
		head = self._desk_head(self.inputs)
		permissions.assert_heads_category(head["name"], self.inputs)
		with self.assertRaises(frappe.PermissionError):
			permissions.assert_heads_category(head["name"], self.credit)

	def test_an_admin_is_not_limited_to_records(self):
		permissions.assert_heads_category("Administrator", self.credit)

	def test_authority_lookups_follow_the_category(self):
		head = self._desk_head(self.inputs)
		self.assertEqual(department_head_of(self.department, category=self.inputs), head["name"])
		self.assertIsNone(department_head_of(self.department, category=self.credit))
