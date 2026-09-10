"""Submitter profile endpoints: options (dropdowns) and current profile (me)."""

from functools import lru_cache

import frappe
from frappe import _
from oan_auth_service.api.utils import handle_api_errors, require_role, success_response

ALLOWED_SUBMITTER_ROLES = [
	"Grievance Submitter",
	"Grievance Officer",
	"Grievance Admin",
	"System Manager",
	"Administrator",
]


def get_jurisdiction_countries() -> list[str]:
	"""Get list of active country names configured in the Administrative Area tree."""
	country_nodes = frappe.get_all(
		"Administrative Area",
		filters={"level_name": "Country", "is_active": 1},
		fields=["area_name", "code", "country"],
		order_by="area_name asc",
		ignore_permissions=True,
	)

	names = set()
	for node in country_nodes:
		if node.get("country"):
			names.add(node["country"])
		if node.get("area_name"):
			names.add(node["area_name"])

	if not names:
		distinct_countries = frappe.get_all(
			"Administrative Area",
			filters={"is_active": 1, "country": ["is", "set"]},
			distinct=True,
			pluck="country",
			ignore_permissions=True,
		)
		names = {c.strip() for c in distinct_countries if c and c.strip()}

	return sorted(names) if names else ["Ethiopia"]


def get_phone_extensions(search: str | None = None, country: str | None = None) -> list[dict]:
	"""Retrieve phone extensions exclusively for active jurisdiction countries in Administrative Area."""
	from frappe.geo.country_info import get_all

	all_geo_data = get_all()
	jurisdiction_names = get_jurisdiction_countries()

	extensions = []
	for j_name in jurisdiction_names:
		info = all_geo_data.get(j_name)
		if not info:
			# Match case-insensitively or via ISO code
			for c_name, c_info in all_geo_data.items():
				if c_name.lower() == j_name.lower() or (c_info.get("code") or "").lower() == j_name.lower():
					info = c_info
					j_name = c_name
					break

		if info and info.get("isd"):
			isd_str = str(info["isd"]).strip()
			if not isd_str.startswith("+"):
				isd_str = f"+{isd_str}"
			extensions.append(
				{
					"country": j_name,
					"code": (info.get("code") or "").upper(),
					"isd": isd_str,
				}
			)

	if not extensions:
		extensions = [{"country": "Ethiopia", "code": "ET", "isd": "+251"}]

	# Sort primary region (Ethiopia) first, then alphabetically
	extensions.sort(key=lambda x: (x["country"] != "Ethiopia", x["country"]))

	if country:
		c_term = country.strip().upper()
		extensions = [e for e in extensions if e["code"] == c_term or e["country"].upper() == c_term]

	if search:
		s_term = search.strip().lower()
		extensions = [
			e
			for e in extensions
			if s_term in e["country"].lower() or s_term in e["code"].lower() or s_term in e["isd"].lower()
		]

	return extensions


def get_status_options() -> list[dict]:
	"""Retrieve grievance lifecycle status options with open/terminal metadata."""
	from oan_grievance_service.services import constants as C

	all_statuses = [
		C.SUBMITTED,
		C.ASSIGNED,
		C.IN_PROGRESS,
		C.MORE_INFO_NEEDED,
		C.PENDING_SUBMITTER,
		C.RESOLVED,
		C.CLOSED,
		C.REJECTED,
	]

	return [
		{
			"status": status,
			"label": status,
			"is_open": 1 if status in C.OPEN_STATUSES else 0,
			"is_terminal": 1 if status in C.TERMINAL_STATUSES else 0,
		}
		for status in all_statuses
	]


@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
@handle_api_errors
def options(
	search_country: str | None = None,
	country: str | None = None,
	include_phone_extensions: bool | str = True,
	service_category: str | None = None,
):
	"""Dropdown options and reference data needed for submitters, intake and public registration.

	Args:
	    search_country (str, optional): Search term to filter country phone extensions (matches name, ISO code, or ISD).
	    country (str, optional): Exact country name or 2-letter ISO code (e.g. 'ET', 'Ethiopia').
	    include_phone_extensions (bool, optional): Whether to include country phone extensions (default True).
	    service_category (str, optional): Filter grievance types by a specific service category (e.g. 'Inputs').

	Returns:
	    submitter_types: Active submitter types (e.g. Individual Farmer, DA, Cooperative, NGO, etc.)
	    submission_types: Active intake channels (e.g. Mobile App, Web Portal, etc.)
	    preferred_languages: Supported notification languages
	    phone_extensions: Country phone extensions / dialing prefixes (ISD codes)
	    statuses: Grievance lifecycle statuses with metadata
	    service_categories: Active service categories (e.g. Inputs, Schemes, Payments, etc.)
	    grievance_types: Active grievance types (optionally filtered by service_category)
	"""
	submitter_types = frappe.get_all(
		"Submitter Type",
		filters={"is_active": 1},
		fields=["name as type_name", "code", "description"],
		order_by="name asc",
		ignore_permissions=True,
	)

	submission_types = frappe.get_all(
		"Submission Type",
		filters={"is_active": 1},
		fields=["name as type_name", "code", "description"],
		order_by="name asc",
		ignore_permissions=True,
	)

	preferred_languages = [
		{"code": "am", "label": "Amharic"},
		{"code": "en", "label": "English"},
	]

	service_categories = frappe.get_all(
		"Service Category",
		filters={"is_active": 1},
		fields=["name as category_name", "code", "sort_order"],
		order_by="sort_order asc, name asc",
		ignore_permissions=True,
	)

	gtype_filters = [["is_active", "=", 1]]
	if service_category:
		gtype_filters.append(["service_category", "=", service_category])

	grievance_types = frappe.get_all(
		"Grievance Type",
		filters=gtype_filters,
		fields=["name as grievance_type_id", "type_name", "service_category"],
		order_by="type_name asc",
		ignore_permissions=True,
	)

	data = {
		"submitter_types": submitter_types,
		"submission_types": submission_types,
		"preferred_languages": preferred_languages,
		"statuses": get_status_options(),
		"service_categories": service_categories,
		"grievance_types": grievance_types,
	}

	should_include_phones = str(include_phone_extensions).lower() not in ("0", "false", "no")
	if should_include_phones:
		data["phone_extensions"] = get_phone_extensions(search=search_country, country=country)

	return success_response(data=data, message=_("Options fetched successfully"))


@frappe.whitelist()
@handle_api_errors
@require_role(ALLOWED_SUBMITTER_ROLES)
def me():
	"""Get the Submitter Profile associated with the currently authenticated user."""
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(_("Authentication required to access current profile."), title=_("Unauthorized"))

	name = frappe.db.get_value("Submitter Profile", {"user": user}, "name")
	if not name:
		frappe.throw(
			_("No submitter profile associated with your user account."), title=_("Profile Not Found")
		)

	doc = frappe.get_doc("Submitter Profile", name)
	scheme = doc.identity_scheme
	ident_val = doc.identity_value

	return success_response(
		data={
			"profile_id": doc.name,
			"identity_scheme": scheme,
			"identity_value": ident_val,
			"fayda_id": ident_val if scheme == "fayda" else None,
			"registration_number": ident_val if scheme == "org" else None,
			"submitter_type": doc.submitter_type,
			"submitter_name": doc.submitter_name,
			"contact_mobile": doc.contact_mobile,
			"contact_email": doc.contact_email,
			# Language lives on the User record, not the profile, so submitters and staff
			# resolve it the same way.
			"preferred_language": frappe.db.get_value("User", user, "language"),
			"administrative_area": getattr(doc, "administrative_area", None),
			"administrative_unit": doc.administrative_unit,
			"active": doc.active,
			"is_blocked": doc.is_blocked,
		},
		message=_("Submitter profile retrieved successfully"),
	)
