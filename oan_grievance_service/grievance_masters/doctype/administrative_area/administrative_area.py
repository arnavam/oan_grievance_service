# Copyright (c) 2026, COSS - Centre for Open Societal Systems and contributors
# For license information, please see license.txt

import frappe
from frappe.utils.nestedset import NestedSet


class AdministrativeArea(NestedSet):
	nsm_parent_field = "parent_administrative_area"

	def autoname(self):
		if getattr(self, "name", None):
			return
		self.set_tree_metadata()
		if self.path_code:
			self.name = self.path_code
		elif self.code and self.parent_administrative_area:
			self.name = f"{self.parent_administrative_area}.{self.code}"
		else:
			self.name = self.area_name

	def validate(self):
		self.set_tree_metadata()

	def set_tree_metadata(self):
		"""Compute depth, country, and materialized path_code."""
		if not self.parent_administrative_area:
			if self.area_name == "World":
				self.depth = 0
				self.is_group = 1
				self.path_code = self.code or "WORLD"
			else:
				self.depth = 0
				self.path_code = self.code or self.area_name
			return

		parent = frappe.get_doc("Administrative Area", self.parent_administrative_area)
		self.depth = (parent.depth or 0) + 1

		# Derive country if not set
		if not self.country:
			if parent.level_name == "Country" or parent.depth == 1:
				self.country = parent.area_name
			elif parent.country:
				self.country = parent.country

		# Materialize path_code: e.g. ET.OROM.BISH.K01
		segment = (self.code or self.area_name).replace(" ", "_").upper()
		if parent.path_code and parent.path_code != "WORLD":
			self.path_code = f"{parent.path_code}.{segment}"
		else:
			self.path_code = segment


def on_doctype_update():
	frappe.db.add_index("Administrative Area", ["lft"])
	frappe.db.add_index("Administrative Area", ["rgt"])
	frappe.db.add_index("Administrative Area", ["path_code"])
	frappe.db.add_index("Administrative Area", ["depth"])
