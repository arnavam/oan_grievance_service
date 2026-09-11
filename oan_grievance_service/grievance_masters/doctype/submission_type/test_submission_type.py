# Copyright (c) 2026, COSS - Centre for Open Societal Systems and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase


class TestSubmissionType(FrappeTestCase):
	def test_submission_type_creation(self):
		if not frappe.db.exists("Submission Type", "Web Portal"):
			doc = frappe.get_doc(
				{
					"doctype": "Submission Type",
					"submission_type_name": "Web Portal",
					"code": "WEB",
					"is_active": 1,
				}
			).insert(ignore_permissions=True)
			self.assertEqual(doc.name, "Web Portal")
