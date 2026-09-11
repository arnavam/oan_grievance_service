# Copyright (c) 2026, COSS - Centre for Open Societal Systems and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.naming import getseries

CODE_LENGTH = 4
MIN_DESCRIPTION_LENGTH = 20


def abbreviate(value: str) -> str:
	"""Uppercase alphanumeric prefix of a name, padded to a fixed width."""
	cleaned = re.sub(r"[^A-Za-z0-9]", "", value or "").upper()
	if not cleaned:
		return "XXXX"
	return cleaned[:CODE_LENGTH].ljust(CODE_LENGTH, "X")


def segment(doctype: str, name: str) -> str:
	"""The configured ticket code for a master record, else an abbreviation of it."""
	if not name:
		return "XXXX"
	try:
		code = frappe.db.get_value(doctype, name, "code")
	except Exception:
		code = None
	return (code or abbreviate(name)).upper()


class Grievance(Document):
	def autoname(self):
		"""Build the ticket number: AREA-CATEGORY-SEQUENCE."""
		area_code = "GEN"
		if self.administrative_area:
			area_code = segment("Administrative Area", self.administrative_area)

		cat_code = segment("Service Category", self.service_category)
		prefix = f"{area_code}-{cat_code}"
		self.name = f"{prefix}-{getseries(prefix + '-', 5)}"
		self.ticket_number = self.name

	def validate(self):
		self.validate_description_length()
		self.validate_grievance_type_category()
		self.set_administrative_area_metadata()

	def set_administrative_area_metadata(self):
		"""Denormalise area_lft and capture immutable area_path_code snapshot."""
		if not self.administrative_area:
			return

		area = frappe.get_doc("Administrative Area", self.administrative_area)
		if area.is_group:
			frappe.throw(
				_(
					"Grievances can only be attached to an operational leaf Administrative Area (not a group)."
				),
				title=_("Invalid Administrative Area"),
			)

		if area.valid_to and str(area.valid_to) <= frappe.utils.today():
			frappe.throw(
				_("The selected Administrative Area '{0}' has been dissolved or reorganized.").format(
					self.administrative_area
				),
				title=_("Dissolved Administrative Area"),
			)

		self.area_lft = area.lft
		if not self.area_path_code:
			self.area_path_code = area.path_code or area.name

	def validate_description_length(self):
		"""FSD 3.2.2: the description is free text with a minimum of 20 characters."""
		description = (self.description or "").strip()
		if len(description) < MIN_DESCRIPTION_LENGTH:
			frappe.throw(
				_("Description must be at least {0} characters.").format(MIN_DESCRIPTION_LENGTH),
				title=_("Description Too Short"),
			)

	def validate_grievance_type_category(self):
		"""FSD 3.2.2: grievance type is loaded per category, so it must belong to one."""
		if not (self.grievance_type and self.service_category):
			return
		parent = frappe.db.get_value("Grievance Type", self.grievance_type, "service_category")
		if parent != self.service_category:
			frappe.throw(
				_("Grievance type {0} belongs to category {1}, not {2}.").format(
					frappe.bold(self.grievance_type),
					frappe.bold(parent),
					frappe.bold(self.service_category),
				),
				title=_("Type Does Not Match Category"),
			)


def on_doctype_update():
	frappe.db.add_index("Grievance", ["area_lft"])
