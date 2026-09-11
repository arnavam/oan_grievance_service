# Copyright (c) 2026, COSS - Centre for Open Societal Systems and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class GrievanceNotificationLog(Document):
	"""FR-08 delivery evidence. Append-only by construction.

	Every role's DocPerm already has delete=0, but DocPerms are bypassed by
	Administrator and by ignore_permissions, so the guard is repeated here. A
	grievance system is asked to prove what it told a citizen and when; a record
	that can be quietly removed cannot do that.
	"""

	def on_trash(self):
		frappe.throw(
			_("Notification log rows are delivery evidence and cannot be deleted."),
			title=_("Not Permitted"),
		)
