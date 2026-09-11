# Copyright (c) 2026, COSS - Centre for Open Societal Systems and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from oan_grievance_service.api.v1.grievance import (
	CLIENT_IMMUTABLE_FIELDS,
	_resolve_submitter_identity,
)
from oan_grievance_service.permissions import grievance_query_conditions, has_grievance_permission
from oan_grievance_service.services import routing


class TestGrievance(FrappeTestCase):
	def setUp(self):
		# Setup tree: Country -> Region -> Woreda (leaf)
		root_name = frappe.db.get_value("Administrative Area", {"area_name": "Tree Root Country"}, "name")
		if not root_name:
			self.root_area = frappe.get_doc(
				{
					"doctype": "Administrative Area",
					"area_name": "Tree Root Country",
					"level_name": "Country",
					"code": "TRC",
					"is_group": 1,
				}
			).insert(ignore_permissions=True)
		else:
			self.root_area = frappe.get_doc("Administrative Area", root_name)

		region_name = frappe.db.get_value("Administrative Area", {"area_name": "Tree Test Region"}, "name")
		if not region_name:
			self.region_area = frappe.get_doc(
				{
					"doctype": "Administrative Area",
					"area_name": "Tree Test Region",
					"level_name": "Region",
					"code": "TTR",
					"parent_administrative_area": self.root_area.name,
					"is_group": 1,
				}
			).insert(ignore_permissions=True)
		else:
			self.region_area = frappe.get_doc("Administrative Area", region_name)

		woreda_name = frappe.db.get_value(
			"Administrative Area", {"area_name": "Tree Test Woreda Leaf"}, "name"
		)
		if not woreda_name:
			self.woreda_leaf = frappe.get_doc(
				{
					"doctype": "Administrative Area",
					"area_name": "Tree Test Woreda Leaf",
					"level_name": "Woreda",
					"code": "TTW",
					"parent_administrative_area": self.region_area.name,
					"is_group": 0,
				}
			).insert(ignore_permissions=True)
		else:
			self.woreda_leaf = frappe.get_doc("Administrative Area", woreda_name)

		self.root_area.reload()
		self.region_area.reload()
		self.woreda_leaf.reload()

		# Ensure masters
		if not frappe.db.exists("Submitter Type", "Individual Farmer"):
			frappe.get_doc(
				{"doctype": "Submitter Type", "type_name": "Individual Farmer", "code": "IND"}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Service Category", "Inputs"):
			frappe.get_doc(
				{"doctype": "Service Category", "category_name": "Inputs", "code": "INPT", "is_active": 1}
			).insert(ignore_permissions=True)

		existing_gtype = frappe.db.get_value("Grievance Type", {"type_name": "Fertilizer Shortage"}, "name")
		if not existing_gtype:
			self.gtype_doc = frappe.get_doc(
				{
					"doctype": "Grievance Type",
					"type_name": "Fertilizer Shortage",
					"service_category": "Inputs",
					"is_active": 1,
				}
			).insert(ignore_permissions=True)
		else:
			self.gtype_doc = frappe.get_doc("Grievance Type", existing_gtype)

		if not frappe.db.exists("Grievance Department", "Agriculture Dept"):
			frappe.get_doc(
				{
					"doctype": "Grievance Department",
					"dept_name": "Agriculture Dept",
					"email_account": "agri@example.com",
					"active": 1,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Grievance Department", "Regional Agronomy Dept"):
			frappe.get_doc(
				{
					"doctype": "Grievance Department",
					"dept_name": "Regional Agronomy Dept",
					"email_account": "regional@example.com",
					"active": 1,
				}
			).insert(ignore_permissions=True)

	def test_grievance_creation_sets_area_lft_and_path_code(self):
		g = frappe.get_doc(
			{
				"doctype": "Grievance",
				"submitter_type": "Individual Farmer",
				"submitter_name": "Tesfaye",
				"contact_mobile": "+251911334455",
				"submission_channel": "Mobile App",
				"administrative_area": self.woreda_leaf.name,
				"service_category": "Inputs",
				"grievance_type": self.gtype_doc.name,
				"description": "Fertilizer subsidy has not been delivered for 3 weeks.",
			}
		).insert(ignore_permissions=True)

		self.assertEqual(g.area_lft, self.woreda_leaf.lft)
		self.assertTrue(bool(g.area_path_code))
		self.assertTrue(g.ticket_number.startswith("TTW-INPT-") or "INPT" in g.ticket_number)

	def test_cannot_attach_grievance_to_group_area(self):
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Grievance",
					"submitter_type": "Individual Farmer",
					"submitter_name": "Tesfaye",
					"contact_mobile": "+251911334455",
					"submission_channel": "Mobile App",
					"administrative_area": self.region_area.name,  # is_group: 1
					"service_category": "Inputs",
					"grievance_type": self.gtype_doc.name,
					"description": "Fertilizer subsidy has not been delivered for 3 weeks.",
				}
			).insert(ignore_permissions=True)

	def test_nearest_ancestor_routing(self):
		# Create a broad rule on Region, and a specific rule on Woreda Leaf
		broad_rule = frappe.get_doc(
			{
				"doctype": "Grievance Routing Rule",
				"rule_precedence": 10,
				"service_category": "Inputs",
				"administrative_area": self.region_area.name,
				"assigned_dept": "Regional Agronomy Dept",
				"active": 1,
			}
		).insert(ignore_permissions=True)

		specific_rule = frappe.get_doc(
			{
				"doctype": "Grievance Routing Rule",
				"rule_precedence": 10,
				"service_category": "Inputs",
				"administrative_area": self.woreda_leaf.name,
				"assigned_dept": "Agriculture Dept",
				"active": 1,
			}
		).insert(ignore_permissions=True)

		g = None
		try:
			g = frappe.get_doc(
				{
					"doctype": "Grievance",
					"submitter_type": "Individual Farmer",
					"submitter_name": "Tesfaye",
					"contact_mobile": "+251911334455",
					"submission_channel": "Mobile App",
					"administrative_area": self.woreda_leaf.name,
					"service_category": "Inputs",
					"grievance_type": self.gtype_doc.name,
					"description": "Fertilizer subsidy has not been delivered for 3 weeks.",
				}
			).insert(ignore_permissions=True)

			matched = routing.find_matching_rule(g)
			self.assertIsNotNone(matched)
			# Specific woreda rule must win over broader region rule because it has narrower span
			self.assertEqual(matched.name, specific_rule.name)
			self.assertEqual(matched.assigned_dept, "Agriculture Dept")
		finally:
			if g and frappe.db.exists("Grievance", g.name):
				frappe.delete_doc("Grievance", g.name, force=True, ignore_permissions=True)
			if frappe.db.exists("Grievance Routing Rule", broad_rule.name):
				frappe.delete_doc(
					"Grievance Routing Rule", broad_rule.name, force=True, ignore_permissions=True
				)
			if frappe.db.exists("Grievance Routing Rule", specific_rule.name):
				frappe.delete_doc(
					"Grievance Routing Rule", specific_rule.name, force=True, ignore_permissions=True
				)


class TestGrievanceSubmitterOwnership(FrappeTestCase):
	"""The submission endpoint owns identity: a caller cannot choose whose case it is.

	`permissions.py` grants read and write on a grievance by matching `submitter`
	against the profiles a user owns, so if the request could set that field a caller
	could file cases attributed to other people.
	"""

	def setUp(self):
		if not frappe.db.exists("Submitter Type", "Individual Farmer"):
			frappe.get_doc(
				{"doctype": "Submitter Type", "type_name": "Individual Farmer", "code": "IND"}
			).insert(ignore_permissions=True)

		self.owner_user = self._user("owner.ownership@example.com", ["Grievance Submitter"])
		self.other_user = self._user("other.ownership@example.com", ["Grievance Submitter"])
		self.officer_user = self._user("officer.ownership@example.com", ["Grievance Officer"])

		self.owner_profile = self._profile(self.owner_user, "Alemayehu Bekele", "+251911000111")
		self.other_profile = self._profile(self.other_user, "Hirut Tadesse", "+251911000222")

		self.addCleanup(frappe.set_user, "Administrator")

	def _user(self, email, roles):
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split(".")[0].title(),
				"send_welcome_email": 0,
				"roles": [{"role": role} for role in roles],
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "User", email, force=True, ignore_permissions=True)
		return user.name

	def _profile(self, user, name, mobile):
		profile = frappe.get_doc(
			{
				"doctype": "Submitter Profile",
				"submitter_type": "Individual Farmer",
				"submitter_name": name,
				"contact_mobile": mobile,
				"user": user,
			}
		).insert(ignore_permissions=True)
		self.addCleanup(
			frappe.delete_doc, "Submitter Profile", profile.name, force=True, ignore_permissions=True
		)
		return profile

	def test_submitter_identity_comes_from_session_not_request(self):
		"""A submitter naming someone else's profile still files as themselves."""
		frappe.set_user(self.owner_user)

		identity = _resolve_submitter_identity(
			{
				"submitter": self.other_profile.name,
				"submitter_name": "Forged Name",
				"contact_mobile": "+251900000000",
			}
		)

		self.assertEqual(identity["submitter"], self.owner_profile.name)
		self.assertEqual(identity["submitter_name"], "Alemayehu Bekele")
		self.assertEqual(identity["contact_mobile"], "+251911000111")

	def test_submitter_cannot_set_assisting_officer(self):
		frappe.set_user(self.owner_user)

		identity = _resolve_submitter_identity({"assisted_by_officer": self.officer_user})

		self.assertIsNone(identity["assisted_by_officer"])

	def test_submitter_without_profile_is_rejected(self):
		orphan = self._user("orphan.ownership@example.com", ["Grievance Submitter"])
		frappe.set_user(orphan)

		with self.assertRaises(frappe.ValidationError):
			_resolve_submitter_identity({})

	def test_blocked_profile_cannot_file(self):
		frappe.db.set_value("Submitter Profile", self.owner_profile.name, "is_blocked", 1)
		frappe.set_user(self.owner_user)

		with self.assertRaises(frappe.ValidationError):
			_resolve_submitter_identity({})

	def test_officer_files_on_behalf_and_is_recorded_as_assisting(self):
		"""The named submitter stays the owner; the officer is the audit trail."""
		frappe.set_user(self.officer_user)

		identity = _resolve_submitter_identity(
			{"submitter": self.other_profile.name, "submitter_name": "Ignored"}
		)

		self.assertEqual(identity["submitter"], self.other_profile.name)
		self.assertEqual(identity["submitter_name"], "Hirut Tadesse")
		self.assertEqual(identity["assisted_by_officer"], self.officer_user)

	def test_officer_may_supply_details_for_a_walk_in_without_a_profile(self):
		frappe.set_user(self.officer_user)

		identity = _resolve_submitter_identity(
			{"submitter_name": "Walk In Caller", "contact_mobile": "+251911000333"}
		)

		self.assertIsNone(identity["submitter"])
		self.assertEqual(identity["submitter_name"], "Walk In Caller")
		self.assertEqual(identity["assisted_by_officer"], self.officer_user)

	def test_server_owned_fields_are_stripped_from_the_request(self):
		"""The area snapshot and ownership columns are not settable by the caller."""
		for field in ("submitter", "assisted_by_officer", "area_lft", "area_path_code", "status"):
			self.assertIn(field, CLIENT_IMMUTABLE_FIELDS)


class TestGrievanceStaffOptions(FrappeTestCase):
	def setUp(self):
		if not frappe.db.exists("Service Category", "Inputs"):
			frappe.get_doc(
				{"doctype": "Service Category", "category_name": "Inputs", "code": "INPT", "is_active": 1}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Grievance Department", "Test Agri Dept"):
			frappe.get_doc(
				{
					"doctype": "Grievance Department",
					"dept_name": "Test Agri Dept",
					"email_account": "test_agri@example.com",
					"active": 1,
				}
			).insert(ignore_permissions=True)

		# Setup officer user
		if frappe.db.exists("User", "officer.options@example.com"):
			frappe.delete_doc("User", "officer.options@example.com", force=True, ignore_permissions=True)
		self.officer_user = frappe.get_doc(
			{
				"doctype": "User",
				"email": "officer.options@example.com",
				"first_name": "Officer",
				"send_welcome_email": 0,
				"roles": [{"role": "Grievance Officer"}],
			}
		).insert(ignore_permissions=True)
		self.addCleanup(
			frappe.delete_doc, "User", "officer.options@example.com", force=True, ignore_permissions=True
		)

		# Setup submitter user
		if frappe.db.exists("User", "submitter.options@example.com"):
			frappe.delete_doc("User", "submitter.options@example.com", force=True, ignore_permissions=True)
		self.submitter_user = frappe.get_doc(
			{
				"doctype": "User",
				"email": "submitter.options@example.com",
				"first_name": "Submitter",
				"send_welcome_email": 0,
				"roles": [{"role": "Grievance Submitter"}],
			}
		).insert(ignore_permissions=True)
		self.addCleanup(
			frappe.delete_doc, "User", "submitter.options@example.com", force=True, ignore_permissions=True
		)

		self.addCleanup(frappe.set_user, "Administrator")

	def test_officer_can_fetch_staff_options(self):
		from oan_grievance_service.api.v1.grievance import options

		frappe.set_user(self.officer_user.name)
		res = options()

		self.assertIn("data", res)
		data = res["data"]

		# Validate departments
		self.assertIn("departments", data)
		dept_names = [d["department_name"] for d in data["departments"]]
		self.assertIn("Test Agri Dept", dept_names)

		# Validate lifecycle statuses with metadata
		self.assertIn("statuses", data)
		status_names = [s["status"] for s in data["statuses"]]
		self.assertIn("Submitted", status_names)
		self.assertIn("In Progress", status_names)
		self.assertIn("Closed", status_names)

		# Validate priorities, categories and channels
		self.assertIn("priorities", data)
		self.assertEqual(data["priorities"], ["Low", "Medium", "High"])
		self.assertIn("service_categories", data)
		self.assertIn("grievance_types", data)
		self.assertIn("submission_channels", data)

	def test_submitter_cannot_access_staff_options(self):
		from oan_grievance_service.api.v1.grievance import options

		frappe.set_user(self.submitter_user.name)
		res = options()
		self.assertEqual(res.get("status"), "error")
		self.assertEqual(res.get("code"), "PERMISSION_DENIED")
		self.assertEqual(frappe.response.get("http_status_code"), 403)
