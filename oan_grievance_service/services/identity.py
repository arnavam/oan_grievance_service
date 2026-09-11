from dataclasses import dataclass

import frappe
from frappe import _

# Identity schemes, matching the dedupe_key prefixes.
SCHEME_FAYDA = "fayda"
SCHEME_ORG = "org"
SCHEME_PHONE = "phone"

# Fields every submitter needs regardless of type
COMMON_REQUIRED = ("submitter_name", "contact_mobile")


@dataclass(frozen=True)
class IdentityRule:
	"""Registration and identity requirements for one submitter type."""

	schemes: tuple
	"""Acceptable dedupe schemes."""

	required: tuple
	"""Fields that must be present before the profile can be saved."""


SUBMITTER_TYPE_RULES = {
	"Individual Farmer": IdentityRule(
		schemes=(SCHEME_FAYDA, SCHEME_PHONE),
		required=COMMON_REQUIRED,
	),
	"Development Agent": IdentityRule(
		schemes=(SCHEME_FAYDA, SCHEME_PHONE),
		required=COMMON_REQUIRED,
	),
	"Cooperative": IdentityRule(
		schemes=(SCHEME_ORG,),
		required=COMMON_REQUIRED,
	),
	"FPO": IdentityRule(
		schemes=(SCHEME_ORG,),
		required=COMMON_REQUIRED,
	),
	"NGO": IdentityRule(
		schemes=(SCHEME_ORG,),
		required=COMMON_REQUIRED,
	),
	"Woreda/Kebele Body": IdentityRule(
		schemes=(SCHEME_ORG,),
		required=COMMON_REQUIRED,
	),
}


def rule_for(submitter_type):
	"""The rule for a type, or None if the type is unknown to this map."""
	return SUBMITTER_TYPE_RULES.get(submitter_type)


def derive_dedupe_key(
	submitter_type: str,
	mobile: str | None = None,
	fayda_id: str | None = None,
	national_id: str | None = None,
	registration_number: str | None = None,
	org_number: str | None = None,
	farmer_id: str | None = None,
	dedupe_key: str | None = None,
) -> str | None:
	"""Automatically derive the canonical scheme-prefixed dedupe key from user inputs.

	Driven directly by the allowed schemes in SUBMITTER_TYPE_RULES:
	- If raw dedupe_key is provided with scheme -> preserves it.
	- If type accepts SCHEME_ORG -> uses registration_number (required if only SCHEME_ORG).
	- If type accepts SCHEME_FAYDA -> uses fayda_id if provided.
	- If type accepts SCHEME_PHONE -> falls back to mobile phone.
	"""
	raw_key = (dedupe_key or "").strip()
	rule = rule_for(submitter_type)
	allowed_schemes = rule.schemes if rule else (SCHEME_FAYDA, SCHEME_ORG, SCHEME_PHONE)

	if raw_key:
		if ":" in raw_key:
			return raw_key
		default_scheme = allowed_schemes[0] if allowed_schemes else SCHEME_PHONE
		return f"{default_scheme}:{raw_key}"

	fayda = (fayda_id or national_id or "").strip()
	org = (registration_number or org_number or "").strip()
	phone = (mobile or "").strip()

	# 1. If type accepts SCHEME_ORG:
	if SCHEME_ORG in allowed_schemes:
		if org:
			return f"{SCHEME_ORG}:{org}"
		# If this type strictly only accepts SCHEME_ORG (e.g. organizations)
		if allowed_schemes == (SCHEME_ORG,):
			frappe.throw(
				_("An official Organization Registration / Certificate Number is required for {0}.").format(
					submitter_type
				),
				frappe.ValidationError,
			)

	# 2. If type accepts SCHEME_FAYDA and fayda ID was provided:
	if SCHEME_FAYDA in allowed_schemes and fayda:
		return f"{SCHEME_FAYDA}:{fayda}"

	# 3. If SCHEME_PHONE is acceptable, fallback to mobile phone
	if SCHEME_PHONE in allowed_schemes and phone:
		return f"{SCHEME_PHONE}:{phone}"

	return None
