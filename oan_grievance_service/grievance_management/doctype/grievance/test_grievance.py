# Copyright (c) 2026, COSS - Centre for Open Societal Systems and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

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
