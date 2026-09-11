# Copyright (c) 2026, COSS - Centre for Open Societal Systems and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from oan_grievance_service.services import identity


class SubmitterProfile(Document):
	def validate(self):
		self.validate_required_identity()
		self.set_dedupe_key()

	def validate_required_identity(self):
		"""Enforce the registration requirements for this submitter type."""
		rule = identity.rule_for(self.submitter_type)
		if not rule:
			frappe.throw(
				_("{0} is not a submitter type this service knows how to register.").format(
					self.submitter_type
				),
				title=_("Unknown Submitter Type"),
			)

		missing = [field for field in rule.required if not (self.get(field) or "").strip()]
		if missing:
			labels = [frappe.unscrub(field) for field in missing]
			frappe.throw(
				_("A {0} must provide: {1}.").format(self.submitter_type, ", ".join(labels)),
				title=_("Incomplete Registration"),
			)

	def set_dedupe_key(self):
		"""Derive or normalize the canonical party key.

		If dedupe_key is provided (e.g. from Fayda / API intake / SSO), ensure it is preserved.
		Otherwise, fallback to phone:<contact_mobile>.
		"""
		key = (self.dedupe_key or "").strip()
		if key:
			if ":" not in key:
				key = build_dedupe_key(identity.SCHEME_PHONE, key)
		elif self.contact_mobile:
			key = build_dedupe_key(identity.SCHEME_PHONE, self.contact_mobile)

		if not key:
			frappe.throw(
				_("A Submitter Profile must have a valid dedupe_key or contact_mobile."),
				title=_("No Identity"),
			)

		# The key is the identity. Letting it move would silently re-point every grievance
		# already filed under it at a different party.
		if not self.is_new() and self.get_doc_before_save() and self.get_doc_before_save().dedupe_key:
			old_key = self.get_doc_before_save().dedupe_key
			if old_key != key:
				frappe.throw(
					_("This profile is already registered as {0} and its identity cannot be changed.").format(
						old_key
					),
					title=_("Identity Is Immutable"),
				)

		self.dedupe_key = key

	@property
	def identity_scheme(self):
		"""Which scheme the party was resolved by: 'fayda', 'org' or 'phone'."""
		return split_dedupe_key(self.dedupe_key)[0]

	@property
	def identity_value(self):
		"""The raw identifier, without its scheme prefix."""
		return split_dedupe_key(self.dedupe_key)[1]


def split_dedupe_key(key):
	"""('fayda', '3214...') from 'fayda:3214...'.

	Split on the first colon only: a phone number in E.164 has no colon, but nothing
	stops a future scheme's value from containing one.
	"""
	if not key or ":" not in key:
		return None, None
	scheme, value = key.split(":", 1)
	return scheme, value


def build_dedupe_key(scheme, value):
	"""The one place a key is composed, so intake and this controller cannot drift."""
	valid_schemes = (identity.SCHEME_FAYDA, identity.SCHEME_ORG, identity.SCHEME_PHONE)
	if scheme not in valid_schemes:
		frappe.throw(_("Unknown identity scheme {0}.").format(scheme))
	return f"{scheme}:{(value or '').strip()}"
