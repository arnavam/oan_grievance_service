# Copyright (c) 2026, COSS - Centre for Open Societal Systems and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase


class TestAdministrativeArea(FrappeTestCase):
	def test_tree_structure_and_lft_rgt(self):
		root_name = frappe.db.get_value("Administrative Area", {"area_name": "Test Country"}, "name")
		if not root_name:
			root = frappe.get_doc(
				{
					"doctype": "Administrative Area",
					"area_name": "Test Country",
					"level_name": "Country",
					"code": "TCT",
					"is_group": 1,
				}
			).insert(ignore_permissions=True)
		else:
			root = frappe.get_doc("Administrative Area", root_name)

		child1_name = frappe.db.get_value("Administrative Area", {"area_name": "Test Region 1"}, "name")
		if not child1_name:
			child1 = frappe.get_doc(
				{
					"doctype": "Administrative Area",
					"area_name": "Test Region 1",
					"level_name": "Region",
					"code": "TR1",
					"parent_administrative_area": root.name,
					"is_group": 1,
				}
			).insert(ignore_permissions=True)
		else:
			child1 = frappe.get_doc("Administrative Area", child1_name)

		child2_name = frappe.db.get_value("Administrative Area", {"area_name": "Test Woreda 1"}, "name")
		if not child2_name:
			child2 = frappe.get_doc(
				{
					"doctype": "Administrative Area",
					"area_name": "Test Woreda 1",
					"level_name": "Woreda",
					"code": "TW1",
					"parent_administrative_area": child1.name,
					"is_group": 0,
				}
			).insert(ignore_permissions=True)
		else:
			child2 = frappe.get_doc("Administrative Area", child2_name)

		root.reload()
		child1.reload()
		child2.reload()

		# Containment checks
		self.assertLess(root.lft, child1.lft)
		self.assertGreater(root.rgt, child1.rgt)
		self.assertLess(child1.lft, child2.lft)
		self.assertGreater(child1.rgt, child2.rgt)

	def test_get_areas_endpoint(self):
		from oan_grievance_service.api.v1.administrative_area import get_areas

		# Default root view returns regions
		res = get_areas()
		self.assertIn("data", res)
		self.assertIn("areas", res["data"])
		self.assertGreaterEqual(res["data"]["count"], 1)

		# Cascading drilldown with parent
		res_child = get_areas(parent="region-ET14")
		self.assertIn("data", res_child)
		self.assertGreaterEqual(res_child["data"]["count"], 1)
		for area in res_child["data"]["areas"]:
			self.assertEqual(area["parent_administrative_area"], "region-ET14")

		# Ancestor breadcrumbs lookup
		res_ancestors = get_areas(ancestors_of="region-ET14")
		self.assertIn("data", res_ancestors)
		self.assertIn("breadcrumbs", res_ancestors["data"])
		self.assertGreaterEqual(len(res_ancestors["data"]["breadcrumbs"]), 1)
