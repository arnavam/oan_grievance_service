# Copyright (c) 2026, COSS - Centre for Open Societal Systems and contributors
# For license information, please see license.txt

"""A position in the escalation chain, as a master rather than an enum.

FSD Appendix F names four rungs - L1, L2, Nodal and Department Head - and
`database-schema.md` carried them as a `role_level` enum on the assignment row. They
live here as records so the ordering is data: escalation walks upward by `level_order`,
and inserting a rung between two others is a row, not a migration.

A level is deliberately not a Role. A Role answers *what actions exist for you* and is
held in `Has Role`; a level answers *where you sit in the chain* and is what escalation
and approval traverse. `database-schema.md` §196 is explicit that holding the same fact
in both places gives two answers to "is this person a nodal officer?" and no rule for
which wins - so the three capability roles stay in `install.py::ROLES`, and seniority
stays here.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class GrievanceRoleLevel(Document):
	def validate(self):
		self.validate_unique_order()

	def validate_unique_order(self):
		"""Two active levels sharing a rank make "the next level up" ambiguous."""
		if not self.is_active:
			return

		clash = frappe.db.exists(
			"Grievance Role Level",
			{
				"level_order": self.level_order,
				"is_active": 1,
				"name": ["!=", self.name],
			},
		)
		if clash:
			frappe.throw(
				_("Active level {0} already uses order {1}. Escalation order must be unambiguous.").format(
					frappe.bold(clash), frappe.bold(self.level_order)
				),
				title=_("Duplicate Level Order"),
			)


def on_doctype_update():
	frappe.db.add_index("Grievance Role Level", ["level_order"])
