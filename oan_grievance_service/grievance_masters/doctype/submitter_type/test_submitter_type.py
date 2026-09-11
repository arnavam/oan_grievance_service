# Copyright (c) 2026, COSS - Centre for Open Societal Systems and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase


class TestSubmitterType(FrappeTestCase):
	def test_submitter_type_creation(self):
		if not frappe.db.exists("Submitter Type", "Individual Farmer"):
			doc = frappe.get_doc(
				{
					"doctype": "Submitter Type",
					"type_name": "Individual Farmer",
					"code": "IND",
					"is_active": 1,
				}
			).insert(ignore_permissions=True)
			self.assertEqual(doc.name, "Individual Farmer")
