"""Grievance submission and submitter actions, exposed for the mobile app, web
portal, IVR and call centre channels.

Every entry point is whitelisted, validates its own input, and routes through the
service layer so the audit trail and notifications cannot be bypassed.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime, validate_phone_number_with_country_code
from oan_auth_service.api.router import prefixed
from oan_auth_service.api.utils import (
	PageParams,
	SafeEmail,
	handle_api_errors,
	page_meta,
	parse_multi_value,
	require_role,
	success_response,
	to_tz_aware_iso,
	validate_request,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from oan_grievance_service import permissions
from oan_grievance_service.api.v1._options import (
	active_channels,
	expand_status_filter,
	get_department_officers,
	get_departments,
	get_grievance_types,
	get_service_categories,
	get_status_options,
	get_status_summary,
	public_status,
)
from oan_grievance_service.api.v1._schemas import Body, NonBlank, blank_to_none
from oan_grievance_service.grievance_management.doctype.grievance.grievance import (
	GrievanceSubmissionPayload,
)
from oan_grievance_service.grievance_management.doctype.grievance_timeline.grievance_timeline import (
	GrievanceTimeline,
)
from oan_grievance_service.services import audit, identity, lifecycle, response_body, routing, sla
from oan_grievance_service.services import constants as C

# Aliased: several entry points take a `ticket_number` argument, which would
# otherwise shadow the module inside them.
from oan_grievance_service.services import ticket_number as tn
from oan_grievance_service.services.resolvers import resolve_administrative_area, resolve_grievance_type

route = prefixed("/api/v1/grievances")

ALLOWED_GRIEVANCE_ROLES = C.ALLOWED_GRIEVANCE_ROLES


class SubmitGrievanceRequest(GrievanceSubmissionPayload):
	"""Request body for the one-step submission endpoint."""

	model_config = ConfigDict(extra="allow")

	submitter_type: str | None = None
	submitter_name: str | None = None
	contact_mobile: str | None = None
	country_code: str | None = None
	phone_number: str | None = None
	phone: str | None = None
	contact_email: SafeEmail | None = None
	submission_channel: str | None = None
	administrative_area: str | None = None
	administrative_unit: str | None = None
	service_category: str | None = None
	grievance_type: str | None = None
	associated_service_provider: str | None = None
	desired_outcome: str | None = None
	is_anonymous: int | bool = 0
	consent_given: int | bool = 1
	anonymity_justification: str | None = None
	client_submission_uuid: str | None = None
	client_uuid: str | None = None


class ListGrievancesRequest(PageParams):
	model_config = ConfigDict(extra="allow")

	limit: int | None = Field(None, ge=1, le=100)
	status: str | list | None = None
	service_category: str | list | None = None
	category: str | list | None = None
	grievance_type: str | list | None = None
	type: str | list | None = None
	assigned_dept: str | list | None = None
	department: str | list | None = None
	dept: str | list | None = None
	assigned_to: str | None = None
	administrative_area: str | list | None = None
	location: str | list | None = None
	region: str | list | None = None
	zone: str | list | None = None
	woreda: str | list | None = None
	kebele: str | list | None = None
	administrative_unit: str | list | None = None
	submission_channel: str | list | None = None
	channel: str | list | None = None
	escalated: bool | str | int | None = None
	is_escalated: bool | str | int | None = None
	is_anonymous: bool | str | int | None = None
	submitter: str | None = None
	from_date: str | None = None
	to_date: str | None = None
	search: str | None = None
	sort_by: str = "creation"
	sort_order: str = "asc"


class GrievanceActionRequest(Body):
	ticket_number: str | None = None
	action: NonBlank = Field(..., description="Workflow action, one of the case's available_actions")
	reason: str | None = Field(
		None, description="Shown to the submitter. For a department response, the response itself."
	)
	action_taken: str | None = Field(
		None,
		description="Department response, first part: what was done. Sent with `resolution_summary` "
		"instead of `reason`; the two are stored together as the reason.",
	)
	resolution_summary: str | None = Field(
		None,
		description="Department response, second part: the outcome for the submitter. "
		"Sent with `action_taken` instead of `reason`.",
	)
	note: str | None = Field(
		None, description="Optional detail recorded directly on the Grievance Status History audit row."
	)
	internal_notes: str | None = Field(
		None, description="Staff only. Posted as a separate internal timeline entry."
	)
	template: str | None = Field(None, description="Staff only. Response template the reason started from.")
	rating: int | None = Field(None, ge=1, le=5, description="Close Case by the submitter only")

	_blank = field_validator(
		"reason", "action_taken", "resolution_summary", "note", "internal_notes", "template", mode="before"
	)(blank_to_none)

	@model_validator(mode="after")
	def check_response_parts(self):
		if self.action_taken is None and self.resolution_summary is None:
			return self
		if self.action_taken is None or self.resolution_summary is None:
			raise ValueError("action_taken and resolution_summary are sent together")
		if self.reason is not None:
			raise ValueError("Send either reason or action_taken with resolution_summary, not both")
		return self


class GrievanceFeedbackRequest(BaseModel):
	model_config = {"extra": "allow"}

	ticket_number: str | None = None
	rating: int = Field(..., ge=1, le=5)
	comments: str | None = None
	feedback_type: str | None = "Resolution"
	feedback_channel: str | None = "Web Portal"


class PostMessageRequest(BaseModel):
	model_config = {"extra": "allow"}

	ticket_number: str | None = None
	body: str = Field(..., min_length=1)
	is_internal: bool | str | None = Field(
		None, description="Hide from the submitter (staff only). Defaults to a public message."
	)


class DecideDeferralRequest(BaseModel):
	decision: str = Field(..., description="Approved or Rejected")
	note: str | None = None


@route(
	"/<ticket_number>/defer-sla/decide",
	methods=("POST",),
	summary="Approve or reject a pending SLA deferral request",
)
@frappe.whitelist()
@validate_request(DecideDeferralRequest)
@handle_api_errors
@require_role(ALLOWED_GRIEVANCE_ROLES)
def decide_deferral(ticket_number: str, decision: str, note: str | None = None, **kwargs):
	"""Approve or reject a pending SLA deferral request for this grievance."""
	doc = _load(ticket_number, ptype="write")
	pending_reqs = frappe.get_all(
		"Grievance Change Request",
		filters={"grievance": doc.name, "status": "Pending"},
		pluck="name",
		order_by="creation desc",
	)
	if not pending_reqs:
		frappe.throw(_("No pending deferral request found for this grievance."), frappe.DoesNotExistError)

	req_items = frappe.get_all(
		"Grievance Change Request Item",
		filters={"parent": ["in", pending_reqs], "fieldname": "sla_due_date"},
		fields=["parent"],
		limit=1,
	)
	if not req_items:
		frappe.throw(_("No pending deferral request found for this grievance."), frappe.DoesNotExistError)

	request_name = req_items[0]["parent"]

	from oan_grievance_service.api.v1.change_request import decide as cr_decide

	return cr_decide(name=request_name, decision=decision, note=note)


class DecideReassignmentRequest(BaseModel):
	decision: str = Field(..., description="Approved or Rejected")
	note: str | None = None


@route(
	"/<ticket_number>/reassign/decide",
	methods=("POST",),
	summary="Approve or reject a pending reassignment request",
)
@frappe.whitelist()
@validate_request(DecideReassignmentRequest)
@handle_api_errors
@require_role(ALLOWED_GRIEVANCE_ROLES)
def decide_reassignment(ticket_number: str, decision: str, note: str | None = None, **kwargs):
	"""Approve or reject a pending reassignment request for this grievance."""
	doc = _load(ticket_number, ptype="write")
	pending_reqs = frappe.get_all(
		"Grievance Change Request",
		filters={"grievance": doc.name, "status": "Pending"},
		pluck="name",
		order_by="creation desc",
	)
	if not pending_reqs:
		frappe.throw(_("No pending reassignment request found for this grievance."), frappe.DoesNotExistError)

	req_items = frappe.get_all(
		"Grievance Change Request Item",
		filters={"parent": ["in", pending_reqs], "fieldname": ["in", ["assigned_dept", "assigned_to"]]},
		fields=["parent"],
		limit=1,
	)
	if not req_items:
		frappe.throw(_("No pending reassignment request found for this grievance."), frappe.DoesNotExistError)

	request_name = req_items[0]["parent"]

	from oan_grievance_service.api.v1.change_request import decide as cr_decide

	return cr_decide(name=request_name, decision=decision, note=note)


class ResponseTemplatesRequest(BaseModel):
	model_config = {"extra": "allow"}

	ticket_number: str | None = None
	action: str = Field(..., min_length=1, description="Workflow action the templates are written for")
	service_category: str | None = Field(None, description="Filter templates by service category")


class ReassignGrievanceRequest(BaseModel):
	model_config = {"extra": "allow"}

	ticket_number: str | None = None
	target_department: str = Field(..., min_length=1)
	target_officer: str | None = None
	target_category: str | None = None
	target_grievance_type: str | None = None
	reason: str | None = None


class DeferSLARequest(BaseModel):
	model_config = {"extra": "allow"}

	ticket_number: str | None = None
	additional_days: int = Field(..., ge=1)
	reason: str = Field(..., min_length=1)


STAFF_ROLES = C.STAFF_ROLES

# Columns the server derives on submission. Accepting any of these from the caller
# would let a client forge case ownership -- permissions.py scopes read and write
# access on `submitter` and `assisted_by_officer` -- or rewrite the filing-time
# area snapshot that Grievance.set_administrative_area_metadata is meant to own.
CLIENT_IMMUTABLE_FIELDS = frozenset(
	{
		"submitter",
		"submitter_name",
		"contact_mobile",
		"contact_email",
		"assisted_by_officer",
		"area_lft",
		"area_path_code",
		"status",
	}
)


def _resolve_submitter_identity(kwargs):
	"""Decide who a grievance belongs to, server-side, and snapshot their profile.

	A submitter always files as themselves: the session decides the owning profile,
	never the request. Staff may file on someone's behalf for the assisted and call
	centre channels, in which case the named submitter stays the owner and the staff
	member is recorded as the assisting officer for the audit trail.

	The name and contact columns are a filing-time copy of the profile, so a case
	keeps showing who reported it even after they later change their phone number.
	Staff taking a walk-in or IVR report from someone with no profile yet supply
	those details directly, because there is nothing to copy from.
	"""
	user = frappe.session.user
	is_staff = permissions.is_staff(user)

	if is_staff:
		profile_name = kwargs.get("submitter")
		identity = {"submitter": profile_name, "assisted_by_officer": user}
	else:
		profile_name = frappe.db.get_value("Grievance Submitter Profile", {"user": user}, "name")
		if not profile_name:
			frappe.throw(
				_("No submitter profile associated with your user account."),
				title=_("Profile Not Found"),
			)
		identity = {"submitter": profile_name, "assisted_by_officer": None}

	if not profile_name:
		identity.update(
			{
				"submitter_name": kwargs.get("submitter_name"),
				"contact_mobile": kwargs.get("contact_mobile"),
				"contact_email": kwargs.get("contact_email"),
			}
		)
		return identity

	profile = frappe.db.get_value(
		"Grievance Submitter Profile",
		profile_name,
		["submitter_type", "submitter_name", "contact_mobile", "contact_email", "active", "is_blocked"],
		as_dict=True,
	)
	if not profile:
		frappe.throw(_("Unknown submitter profile."), title=_("Invalid Submitter"))

	# The schema records is_blocked as blocking new submissions while leaving existing
	# cases visible, so it is enforced here rather than in the permission layer.
	if profile.is_blocked or not profile.active:
		frappe.throw(
			_("This submitter profile cannot file new grievances."),
			title=_("Submitter Blocked"),
		)

	identity.update(
		{
			"submitter_type": profile.submitter_type,
			"submitter_name": profile.submitter_name,
			"contact_mobile": profile.contact_mobile,
			"contact_email": profile.contact_email,
		}
	)
	return identity


def detect_duplicates(grievance, window_days=7):
	"""Match on submitter identity, grievance type and time proximity."""
	if not grievance.submitter:
		return []

	candidates = frappe.get_all(
		"Grievance",
		filters={
			"name": ["!=", grievance.name],
			"submitter": grievance.submitter,
			"grievance_type": grievance.grievance_type,
			"creation": [">=", frappe.utils.add_days(now_datetime(), -window_days)],
			"docstatus": ["!=", 0],
		},
		pluck="name",
	)

	rows = []
	for candidate in candidates:
		rows.append(
			frappe.get_doc(
				{
					"doctype": "Grievance Duplicate",
					"grievance": grievance.name,
					"duplicate_of": candidate,
					"detected_at": now_datetime(),
					"detection_method": "Identity + Type + Time Proximity",
					"similarity_score": 1.0,
				}
			).insert(ignore_permissions=True)
		)
	return rows


def _resolve_area_filter_identifiers(identifier: str, level_hint: str | None = None) -> list[str]:
	"""Resolve an area identifier, path_code, code, or area_name to matching canonical doc names."""
	if not identifier:
		return []
	identifier = str(identifier).strip()
	if frappe.db.exists("Grievance Administrative Area", identifier):
		return [identifier]

	by_path = frappe.get_all("Grievance Administrative Area", filters={"path_code": identifier}, pluck="name")
	if by_path:
		return by_path

	by_code = frappe.get_all("Grievance Administrative Area", filters={"code": identifier}, pluck="name")
	if by_code:
		return by_code

	if level_hint:
		by_level = frappe.get_all(
			"Grievance Administrative Area",
			filters={"area_name": identifier, "level_name": level_hint},
			pluck="name",
		)
		if by_level:
			return by_level

	by_region = frappe.get_all(
		"Grievance Administrative Area",
		filters={"area_name": identifier, "level_name": "Region"},
		pluck="name",
	)
	if by_region:
		return by_region

	by_name = frappe.get_all("Grievance Administrative Area", filters={"area_name": identifier}, pluck="name")
	if by_name:
		return by_name

	frappe.throw(
		_("Administrative area '{0}' could not be resolved.").format(identifier),
		frappe.DoesNotExistError,
		title=_("Invalid Area Filter"),
	)


def _resolve_area_filter_identifier(identifier: str, level_hint: str | None = None) -> str:
	"""Resolve an area identifier, path_code, code, or area_name for filtering (single name)."""
	matches = _resolve_area_filter_identifiers(identifier, level_hint)
	return matches[0] if matches else ""


@route("", methods=("GET",), summary="List grievances with filtering, pagination, and sorting")
@frappe.whitelist()
@validate_request(ListGrievancesRequest)
@handle_api_errors
@require_role(ALLOWED_GRIEVANCE_ROLES)
def list_grievances(
	page: int = 1,
	page_size: int = 20,
	limit: int | None = None,
	status: str | list | None = None,
	service_category: str | list | None = None,
	category: str | list | None = None,
	grievance_type: str | list | None = None,
	type: str | list | None = None,
	assigned_dept: str | list | None = None,
	department: str | list | None = None,
	dept: str | list | None = None,
	assigned_to: str | None = None,
	administrative_area: str | list | None = None,
	location: str | list | None = None,
	region: str | list | None = None,
	zone: str | list | None = None,
	woreda: str | list | None = None,
	kebele: str | list | None = None,
	administrative_unit: str | list | None = None,
	submission_channel: str | list | None = None,
	channel: str | list | None = None,
	escalated: bool | str | int | None = None,
	is_escalated: bool | str | int | None = None,
	is_anonymous: bool | str | int | None = None,
	submitter: str | None = None,
	from_date: str | None = None,
	to_date: str | None = None,
	search: str | None = None,
	sort_by: str = "creation",
	sort_order: str = "asc",
	**kwargs,
):
	"""Retrieve paginated and filtered list of grievances.

	Enforces deny-by-default RBAC through permission query conditions:
	- Grievance Submitters only see their own cases and assisted submissions.
	- Grievance Officers only see cases matching their RBAC scope (administrative area subtree,
	  department, category) or directly assigned to them.
	- Grievance Admins and System Managers see all cases.

	Supports multi-select values (list, JSON array, or comma-separated string) for status,
	service_category/category, administrative_area/location/region/zone/woreda/kebele,
	grievance_type, department, and submission_channel.

	Status filters use the queue cards (All, In Progress, Require More Info, Rejected,
	Resolved, Closed; officers also get Assigned). Draft is never returned. Workflow
	stages that are not a card are reported as the card they roll up into.
	"""
	effective_limit = limit if limit is not None else page_size
	offset = (page - 1) * effective_limit

	filters = [
		["docstatus", "!=", 0],
	]

	status_raw = next((val for val in (status, kwargs.get("status")) if val is not None), None)
	status_vals = parse_multi_value(status_raw)
	if status_vals:
		expanded = expand_status_filter(status_vals)
		if expanded:
			filters.append(["status", "in", expanded])

	MULTI_SELECT_FIELDS = {
		"service_category": [service_category, category, kwargs.get("category")],
		"grievance_type": [grievance_type, type, kwargs.get("type")],
		"assigned_dept": [assigned_dept, department, dept, kwargs.get("dept"), kwargs.get("department")],
		"submission_channel": [submission_channel, channel, kwargs.get("channel")],
	}
	for fieldname, candidates in MULTI_SELECT_FIELDS.items():
		raw = next((val for val in candidates if val is not None), None)
		vals = parse_multi_value(raw)
		if vals:
			filters.append([fieldname, "in", vals])

	if assigned_to:
		target_user = frappe.session.user if assigned_to == "me" else assigned_to
		filters.append(["assigned_to", "=", target_user])

	if submitter:
		if submitter == "me":
			profile_name = frappe.db.get_value(
				"Grievance Submitter Profile", {"user": frappe.session.user}, "name"
			)
			if profile_name:
				filters.append(["submitter", "=", profile_name])
		else:
			filters.append(["submitter", "=", submitter])

	if is_anonymous is not None:
		val = 1 if str(is_anonymous).lower() in ("1", "true", "yes") else 0
		filters.append(["is_anonymous", "=", val])

	esc = is_escalated if is_escalated is not None else escalated
	if esc is not None:
		val = 1 if str(esc).lower() in ("1", "true", "yes") else 0
		filters.append(["escalated", "=", val])

	if from_date:
		filters.append(["creation", ">=", f"{from_date} 00:00:00" if len(from_date) == 10 else from_date])

	if to_date:
		filters.append(["creation", "<=", f"{to_date} 23:59:59" if len(to_date) == 10 else to_date])

	tier_filters = [
		(kebele or kwargs.get("kebele"), "Kebele"),
		(woreda or kwargs.get("woreda"), "Woreda"),
		(zone or kwargs.get("zone"), "Zone"),
		(region or kwargs.get("region"), "Region"),
		(location or kwargs.get("location"), None),
		(administrative_area or kwargs.get("administrative_area"), None),
		(administrative_unit or kwargs.get("administrative_unit"), None),
	]

	active_tier_area_sets = []
	for val, lvl in tier_filters:
		if val is not None:
			raw_items = parse_multi_value(val)
			if not raw_items:
				continue
			tier_area_names = set()
			canonical_names = set()
			for item in raw_items:
				canonical_names.update(_resolve_area_filter_identifiers(item, lvl))

			if canonical_names:
				bounds_rows = frappe.get_all(
					"Grievance Administrative Area",
					filters={"name": ["in", list(canonical_names)]},
					fields=["name", "lft", "rgt", "is_group"],
				)
				range_filters = []
				for b in bounds_rows:
					if b.lft is not None and b.rgt is not None and (b.get("is_group") or (b.rgt - b.lft > 1)):
						range_filters.append([["lft", ">=", int(b.lft)], ["lft", "<=", int(b.rgt)]])
					else:
						tier_area_names.add(b.name)

				for rf in range_filters:
					descendants = frappe.get_all(
						"Grievance Administrative Area",
						filters=rf,
						pluck="name",
					)
					tier_area_names.update(descendants)

			if tier_area_names:
				active_tier_area_sets.append(tier_area_names)

	if active_tier_area_sets:
		matching_areas = active_tier_area_sets[0]
		for s in active_tier_area_sets[1:]:
			matching_areas = matching_areas.intersection(s)

		if not matching_areas:
			# The area filters exclude each other, so nothing can match: skip the query.
			audit.record_access(audit.ACTION_VIEW_LIST)
			return _grievance_page([], page, effective_limit, total_count=0)
		filters.append(["administrative_area", "in", list(matching_areas)])

	or_filters = []
	if search:
		term = search.strip()
		search_pattern = f"%{term}%"
		matching_types = frappe.get_all(
			"Grievance Type",
			filters={"type_name": ["like", search_pattern]},
			pluck="name",
		)
		or_filters = [
			["ticket_number", "like", search_pattern],
			["name", "like", search_pattern],
			["submitter_name", "like", search_pattern],
		]
		# A ticket is printed grouped and read back over a phone line, but
		# stored flat, so a pasted `B-001-0012-0` matches nothing on the raw
		# pattern alone. Added beside the raw clause rather than replacing it:
		# grievances numbered under the old hyphenated format carry real hyphens in the stored
		# value, and cleaning the term would stop those matching.
		ticket_term = tn.clean(term)
		if ticket_term and ticket_term != term:
			or_filters.append(["ticket_number", "like", f"%{ticket_term}%"])
			or_filters.append(["name", "like", f"%{ticket_term}%"])
		if matching_types:
			or_filters.append(["grievance_type", "in", matching_types])
		else:
			or_filters.append(["grievance_type", "like", search_pattern])

	allowed_sort_fields = {
		"creation",
		"modified",
		"ticket_number",
		"status",
		"sla_due_date",
		"service_category",
		"grievance_type",
		"escalated",
	}
	order_field = sort_by if sort_by in allowed_sort_fields else "creation"
	order_direction = "desc" if str(sort_order).lower() == "desc" else "asc"
	if order_field == "escalated":
		order_by = f"`tabGrievance`.escalated {order_direction}, `tabGrievance`.creation asc"
	else:
		order_by = f"`tabGrievance`.escalated desc, `tabGrievance`.{order_field} {order_direction}"

	fields = [
		"name",
		"ticket_number",
		"status",
		"escalated",
		"submission_channel",
		"submitter",
		"submitter_name",
		"contact_mobile",
		"contact_email",
		"is_anonymous",
		"administrative_area",
		"service_category",
		"grievance_type",
		"description",
		"assigned_dept",
		"assigned_to",
		"sla_due_date",
		"state_deadline",
		"creation as submitted_on",
		"modified as updated_at",
	]

	items = frappe.get_list(
		"Grievance",
		filters=filters,
		or_filters=or_filters if or_filters else None,
		fields=fields,
		order_by=order_by,
		start=offset,
		limit=effective_limit,
	)

	total_records = frappe.get_list(
		"Grievance",
		filters=filters,
		or_filters=or_filters if or_filters else None,
		fields=[{"COUNT": "*", "as": "total"}],
		limit=1,
	)
	total_count = int(total_records[0].get("total", 0)) if total_records else 0

	from oan_grievance_service.api.v1.administrative_area import (
		format_administrative_location,
		get_administrative_hierarchy,
	)

	area_cache = {}
	unique_areas = {item.get("administrative_area") for item in items if item.get("administrative_area")}
	for area_id in unique_areas:
		h = get_administrative_hierarchy(area_id)
		loc = format_administrative_location(h)
		area_cache[area_id] = {"hierarchy": h, "location": loc}

	for item in items:
		item["escalated"] = bool(item.get("escalated"))
		state_deadline = item.pop("state_deadline", None)
		item["confirmation_deadline"] = state_deadline if item.get("status") == C.STATE_RESOLVED else None

		show_id = permissions.can_see_identity(item)
		contact = identity.mask_contact(item, show_identity=show_id)
		item.update(contact)
		if not show_id:
			item["submitter_name"] = _("Anonymous Submitter")

		item["is_anonymous"] = bool(item.get("is_anonymous"))
		item["status"] = public_status(item.get("status"))
		item["department"] = item.get("assigned_dept")
		# Grouped for reading, as `timeline` returns it. Stored flat in DB,
		# but exposed formatted to clients as ticket_number.
		item["ticket_number"] = tn.display(item.get("ticket_number"))
		area_info = area_cache.get(item.get("administrative_area"))
		item["location"] = area_info["location"] if area_info else None
		item["administrative_hierarchy"] = area_info["hierarchy"] if area_info else None

	audit.record_access(audit.ACTION_VIEW_LIST)

	return _grievance_page(items, page, effective_limit, total_count)


def _grievance_page(items, page, page_size, total_count):
	"""The list endpoint's envelope, shared by the normal and the short-circuit path."""
	meta = page_meta(total_count, page, page_size)
	return success_response(
		data={
			"items": items,
			"pagination": meta,
		},
		pagination=meta,
		message=_("Grievances retrieved successfully"),
	)


ACTIONS_REQUIRING_REASON = {
	"Close Case",
	"Partially Resolve",
	"Refer Onward",
	"Reject",
	"Reopen",
	"Request More Info",
	"Resolve",
	"Submitter Reply",
}

ACTION_CODES = {
	"Close Case": "close_case",
	"Reopen": "reopen",
	"Resolve": "resolve",
	"Partially Resolve": "partially_resolve",
	"Reject": "reject",
	"Refer Onward": "refer_onward",
	"Request More Info": "request_more_info",
	"Submitter Reply": "submitter_reply",
}

ACTIONS_REQUIRING_RATING = {"Close Case"}


def _get_available_actions_for_user(doc):
	"""List actions available to the current user on this grievance with localized labels."""
	user = frappe.session.user
	is_staff = permissions.is_staff(user)

	actions = lifecycle.actions_available(doc)
	result = []
	for act in actions:
		if act == "Reject" and not is_staff:
			continue
		if act == "Assign":
			continue
		result.append(
			{
				"action": act,
				"action_code": ACTION_CODES.get(act, frappe.scrub(act)),
				"label": _(act),
				"requires_reason": act in ACTIONS_REQUIRING_REASON,
				"requires_rating": act in ACTIONS_REQUIRING_RATING,
			}
		)

	return result


def _current_state(doc, extra=None):
	"""The `current_state` block every mutating endpoint echoes back.

	One definition, so a field added for one endpoint's client is there for all of
	them and the shape cannot drift between them. `extra` carries the fields only
	one endpoint has a reason to report.
	"""
	state = {
		"status": doc.status,
		"escalated": bool(doc.escalated),
		"assigned_to": doc.assigned_to,
		"department": doc.assigned_dept,
		"updated_at": to_tz_aware_iso(doc.modified),
		"available_actions": _get_available_actions_for_user(doc),
		"can_request_more_info": identity.is_reachable(doc),
	}
	if extra:
		state.update(extra)
	return state


@route("", methods=("POST",), summary="Submit a new grievance")
@frappe.whitelist()
@validate_request(SubmitGrievanceRequest)
@handle_api_errors
@require_role(ALLOWED_GRIEVANCE_ROLES)
def submit(**kwargs):
	"""Submit a grievance in one request using the current draft flow."""
	from oan_grievance_service.api.v1 import draft

	client_submission_uuid = (
		kwargs.get("client_submission_uuid") or kwargs.get("client_uuid") or frappe.generate_hash(length=32)
	)
	fields = (
		"submission_channel",
		"submitter_type",
		"submitter_name",
		"contact_mobile",
		"country_code",
		"phone_number",
		"phone",
		"contact_email",
		"administrative_area",
		"administrative_unit",
		"service_category",
		"grievance_type",
		"associated_service_provider",
		"description",
		"desired_outcome",
		"is_anonymous",
		"can_request_more_info",
	)
	save_kwargs = {field: kwargs[field] for field in fields if kwargs.get(field) is not None}
	draft.save(client_submission_uuid=client_submission_uuid, **save_kwargs)

	return draft.submit_draft(
		client_submission_uuid=client_submission_uuid,
		consent_given=kwargs.get("consent_given", 1),
		is_anonymous=kwargs.get("is_anonymous", 0),
		anonymity_justification=kwargs.get("anonymity_justification"),
		can_request_more_info=kwargs.get("can_request_more_info", True),
	)


def resolve_timeline_author(
	author_submitter: str | None, author_user: str | None, submitter_type: str | None = None
) -> tuple[str, str]:
	"""Determine author_type and author_role for timeline formatting."""
	if author_submitter:
		return "submitter", submitter_type or "Grievance Submitter"
	if author_user:
		from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
			current_level_of,
		)

		level_id = current_level_of(author_user)
		role_name = (
			frappe.db.get_value("Grievance Role Level", level_id, "level_name") if level_id else None
		) or "Grievance Officer"
		return "officer", role_name
	return "system", "System"


def _actions_for(history_names):
	"""The workflow action each status history row was taken under, keyed by row name."""
	if not history_names:
		return {}
	return dict(
		frappe.get_all(
			"Grievance Status History",
			filters={"name": ["in", list(history_names)], "action": ["is", "set"]},
			fields=["name", "action"],
			as_list=True,
		)
	)


def _format_timeline_event(entry, doc, from_status=None, to_status=None):
	if not entry:
		return None
	author_submitter = (
		getattr(entry, "author_submitter", None)
		if hasattr(entry, "author_submitter")
		else entry.get("author_submitter")
	)
	author_user = (
		getattr(entry, "author_user", None) if hasattr(entry, "author_user") else entry.get("author_user")
	)
	author_type, author_role = resolve_timeline_author(author_submitter, author_user, doc.submitter_type)

	created_val = (
		getattr(entry, "created_on", None) if hasattr(entry, "created_on") else entry.get("created_on")
	)
	body = getattr(entry, "body", None) if hasattr(entry, "body") else entry.get("body")

	return {
		"id": getattr(entry, "name", None) if hasattr(entry, "name") else entry.get("name"),
		"entry_type": getattr(entry, "entry_type", None)
		if hasattr(entry, "entry_type")
		else entry.get("entry_type"),
		"body": body,
		"body_parts": response_body.split(body),
		"is_internal": bool(
			getattr(entry, "is_internal", False)
			if hasattr(entry, "is_internal")
			else entry.get("is_internal")
		),
		"author_role": author_role,
		"author_type": author_type,
		"from_status": from_status,
		"to_status": to_status or doc.status,
		"created_on": to_tz_aware_iso(created_val),
		"action": _actions_for([entry.get("ref_docname")] if entry.get("ref_docname") else []).get(
			entry.get("ref_docname")
		),
	}


# The timeline entry an action writes, where it is not a plain status change.
_ACTION_ENTRY_TYPES = {
	"Close Case": "resolution",
	"Reject": "rejection",
	"Request More Info": "info_request",
	"Submitter Reply": "info_response",
	"Resolve": "response",
	"Partially Resolve": "response",
	"Refer Onward": "response",
}


@route("/<ticket_number>/action", methods=("POST",), summary="Execute a workflow action on a grievance")
@frappe.whitelist()
@validate_request(GrievanceActionRequest)
@handle_api_errors
@require_role(ALLOWED_GRIEVANCE_ROLES)
def action(
	ticket_number: str,
	action: str,
	reason: str | None = None,
	action_taken: str | None = None,
	resolution_summary: str | None = None,
	note: str | None = None,
	internal_notes: str | None = None,
	template: str | None = None,
	rating: int | None = None,
	**kwargs,
):
	"""Take a workflow action on a grievance.

	Every action carries a `reason`, which the submitter sees. A department response
	may instead come as `action_taken` and `resolution_summary`, stored together as
	the reason. Which actions the caller may take from the case's state is the
	Grievance Workflow's call. Files sent as multipart attach to the action's
	timeline entry.
	"""
	if action_taken is not None or resolution_summary is not None:
		if not (action_taken and action_taken.strip() and resolution_summary and resolution_summary.strip()):
			frappe.throw(
				_(
					"Both 'action_taken' and 'resolution_summary' are required when submitting a two-part resolution."
				),
				frappe.ValidationError,
				title=_("Resolution Details Required"),
			)
		reason = response_body.compose(action_taken, resolution_summary)
	doc = _load(ticket_number, ptype="write")
	from_status = doc.status
	user = frappe.session.user
	is_staff = permissions.is_staff(user)

	matching_action = next((a for a in lifecycle.actions_available(doc) if a.lower() == action.lower()), None)
	if not matching_action:
		frappe.throw(
			_("Action '{0}' is not available for this grievance in status '{1}'.").format(action, doc.status),
			frappe.ValidationError,
			title=_("Action Not Permitted"),
		)
	if not is_staff and (internal_notes or template):
		frappe.throw(
			_("Only staff members can add internal notes or use a response template."),
			frappe.PermissionError,
		)

	if matching_action in ACTIONS_REQUIRING_REASON and (not reason or not reason.strip()):
		frappe.throw(
			_("A reason is required for action '{0}'.").format(matching_action),
			frappe.ValidationError,
			title=_("Reason Required"),
		)

	# Files are checked before the move so a bad one refuses the whole request.
	from oan_grievance_service.api.v1.attachment import prepare_uploads, store_uploads

	prepared = prepare_uploads(doc.name)

	if matching_action == "Assign":
		frappe.throw(
			_(
				"Direct 'Assign' action is not permitted on this endpoint. Use the assignment/reassignment API to assign a department and officer."
			),
			frappe.ValidationError,
			title=_("Action Not Permitted"),
		)

	if matching_action == "Start Work":
		_ensure_department(doc)

	# Submitter closing the case accepts and confirms the resolution.
	confirmed = matching_action == "Close Case"
	history = lifecycle.transition(doc, matching_action, reason=reason, note=note, confirmed=confirmed)
	if matching_action == "Close Case":
		doc.db_set("closure_reason", reason, update_modified=False)
	elif matching_action == "Reopen":
		doc.db_set("reopen_count", (doc.reopen_count or 0) + 1, update_modified=False)
	elif matching_action == "Submitter Reply":
		from oan_grievance_service.services import notifications

		notifications.queue(doc, C.EVENT_SUBMITTER_RESPONDED)

	timeline_entry = GrievanceTimeline.record(
		grievance=doc.name,
		entry_type=_ACTION_ENTRY_TYPES.get(matching_action, "status_change"),
		is_internal=False,
		body=reason or _("Action: {0}").format(matching_action),
		author_user=user if is_staff else None,
		author_submitter=None if is_staff else doc.submitter,
		ref_doctype="Grievance Status History",
		ref_docname=history.name if history else None,
	)
	if internal_notes:
		GrievanceTimeline.record(
			grievance=doc.name,
			entry_type="note",
			is_internal=True,
			body=internal_notes.strip(),
			author_user=user,
		)
	if template and frappe.db.exists("Grievance Response Template", template):
		from oan_grievance_service.grievance_masters.doctype.grievance_response_template.grievance_response_template import (
			record_use,
		)

		record_use(template)
	# Ratings normally arrive later through /feedback. One the submitter sends with the
	# close is recorded the same way; the reason is already on the close entry.
	if rating is not None and confirmed:
		frappe.get_doc(
			{
				"doctype": "Grievance Feedback",
				"grievance": doc.name,
				"rating": int(rating),
				"feedback_type": "Resolution",
				"submitted_by": user,
				"author_submitter": doc.submitter,
				"submitted_at": now_datetime(),
			}
		).insert(ignore_permissions=True)

	attachments = store_uploads(doc, prepared, timeline_entry=timeline_entry.name) if prepared else []

	doc.reload()
	current_state = _current_state(doc)
	return success_response(
		data={
			"ticket_number": tn.display(doc.ticket_number),
			"status": doc.status,
			"action": matching_action,
			"attachments": attachments,
			"current_state": current_state,
			"timeline_event": _format_timeline_event(timeline_entry, doc, from_status, doc.status),
			"available_actions": current_state["available_actions"],
		},
		message=_("Grievance updated successfully"),
	)


def _ensure_department(doc):
	"""Work starts in a department: fall back to the assigned officer's, else refuse."""
	if doc.assigned_dept:
		return
	if doc.assigned_to:
		from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
			active_scopes,
		)

		scopes = active_scopes(doc.assigned_to)
		if scopes and scopes[0].get("department_scope"):
			doc.db_set("assigned_dept", scopes[0].get("department_scope"), update_modified=False)
	if not doc.assigned_dept:
		frappe.throw(
			_("Cannot start work on a grievance without an assigned department."),
			frappe.ValidationError,
			title=_("Department Required"),
		)


@route(
	"/<ticket_number>/feedback", methods=("POST",), summary="Submit citizen feedback / satisfaction rating"
)
@frappe.whitelist()
@validate_request(GrievanceFeedbackRequest)
@handle_api_errors
@require_role(ALLOWED_GRIEVANCE_ROLES)
def feedback(
	ticket_number: str,
	rating: int,
	comments: str | None = None,
	feedback_type: str = "Resolution",
	feedback_channel: str = "Web Portal",
	**kwargs,
):
	"""Submit satisfaction rating and feedback for a resolved or closed grievance."""
	doc = _load(ticket_number, ptype="read")
	user = frappe.session.user

	submitter_profile = None
	if not permissions.is_staff(user):
		from oan_grievance_service.grievance_management.doctype.grievance_submitter_profile.grievance_submitter_profile import (
			profiles_of,
		)

		user_profiles = profiles_of(user)
		if doc.submitter in user_profiles:
			submitter_profile = doc.submitter
		elif user_profiles:
			submitter_profile = user_profiles[0]

	ip = getattr(frappe.local, "request_ip", None)

	fb_doc = frappe.get_doc(
		{
			"doctype": "Grievance Feedback",
			"grievance": doc.name,
			"rating": int(rating),
			"feedback_type": feedback_type,
			"feedback_channel": feedback_channel,
			"comments": comments.strip() if comments else None,
			"submitted_by": user,
			"author_submitter": submitter_profile,
			"submitted_at": now_datetime(),
			"ip_address": ip,
		}
	).insert(ignore_permissions=True)

	return success_response(
		data={
			"ticket_number": doc.ticket_number or doc.name,
			"feedback_id": fb_doc.name,
			"rating": fb_doc.rating,
			"comments": fb_doc.comments,
			"submitted_at": to_tz_aware_iso(fb_doc.submitted_at),
		},
		message=_("Feedback submitted successfully"),
	)


@route("/<ticket_number>/timeline", methods=("GET",), summary="Get grievance timeline and thread details")
@frappe.whitelist()
@handle_api_errors
@require_role(ALLOWED_GRIEVANCE_ROLES)
def timeline(
	ticket_number: str,
	is_internal: bool | str | None = None,
	limit: int | str = 20,
	cursor: str | None = None,
):
	"""Retrieve chronological unified conversation, activity timeline, and thread summary for a grievance.

	Submitters only see public entries (is_internal = 0).
	Staff (Officers, Admins) see all entries or can filter by is_internal flag.
	"""
	doc = _load(ticket_number)
	doc.check_permission("read")
	audit.record_access(audit.ACTION_VIEW_DETAIL, grievance=doc.name)

	user = frappe.session.user
	is_staff = permissions.is_staff(user)

	filters = {"grievance": doc.name}

	if not is_staff:
		filters["is_internal"] = 0
	elif is_internal is not None:
		filters["is_internal"] = 1 if str(is_internal).lower() in ("1", "true", "yes") else 0

	if cursor:
		filters["created_on"] = ["<", cursor]

	page_limit = max(1, min(int(limit), 100))
	entries = frappe.get_all(
		"Grievance Timeline",
		filters=filters,
		fields=[
			"name",
			"entry_type",
			"is_internal",
			"body",
			"author_user",
			"author_submitter",
			"ref_doctype",
			"ref_docname",
			"created_on",
		],
		order_by="created_on desc, name desc",
		limit=page_limit + 1,
	)

	has_more = len(entries) > page_limit
	if has_more:
		entries = entries[:page_limit]

	next_cursor = to_tz_aware_iso(entries[-1]["created_on"]) if (has_more and entries) else None

	show_identity = permissions.can_see_identity(doc)
	contact = identity.mask_contact(doc, show_identity=show_identity)
	masked_name = doc.submitter_name if show_identity else _("Anonymous Submitter")

	actions = _actions_for(
		{e["ref_docname"] for e in entries if e.get("ref_doctype") == "Grievance Status History"}
	)
	for entry in entries:
		entry["action"] = actions.get(entry.get("ref_docname"))
		entry["is_internal"] = bool(entry.get("is_internal"))
		author_type, author_role = resolve_timeline_author(
			entry.get("author_submitter"), entry.get("author_user"), doc.submitter_type
		)
		entry["author_type"] = author_type
		entry["author_role"] = author_role
		entry["created_on"] = to_tz_aware_iso(entry.get("created_on"))
		entry["body_parts"] = response_body.split(entry.get("body"))

	# Only scanned evidence is listed. Files attached straight to the case used
	# to be shown beside these rows without a verdict; the
	# link_attachments_to_files patch turned them into attachment rows.
	grievance_attachments = frappe.get_all(
		"Grievance Attachment",
		filters={"grievance": doc.name},
		fields=[
			"name",
			"file_name",
			"file_url",
			"size_bytes as file_size",
			"mime_type",
			"document_type",
			"scan_status",
			"timeline_entry",
			"uploaded_by_user",
			"uploaded_by_submitter",
			"creation",
		],
		order_by="creation asc",
		ignore_permissions=True,
	)
	attachments = list(grievance_attachments)

	attachments_by_timeline = {}
	for att in grievance_attachments:
		tl_id = att.get("timeline_entry")
		if tl_id:
			attachments_by_timeline.setdefault(tl_id, []).append(att)

	for entry in entries:
		entry["attachments"] = attachments_by_timeline.get(entry["name"], [])

	pending_reqs = frappe.get_all(
		"Grievance Change Request",
		filters={"grievance": doc.name, "status": "Pending"},
		pluck="name",
		order_by="creation desc",
	)
	active_deferral_request = None
	active_reassignment_request = None
	if pending_reqs:
		req_items = frappe.get_all(
			"Grievance Change Request Item",
			filters={"parent": ["in", pending_reqs]},
			fields=["parent", "fieldname"],
		)
		deferral_req_names = {item["parent"] for item in req_items if item["fieldname"] == "sla_due_date"}
		reassign_req_names = {
			item["parent"] for item in req_items if item["fieldname"] in ("assigned_dept", "assigned_to")
		}

		from oan_grievance_service.api.v1.change_request import serialize as serialize_cr

		for req_name in pending_reqs:
			if req_name in deferral_req_names and not active_deferral_request:
				active_deferral_request = serialize_cr(frappe.get_doc("Grievance Change Request", req_name))
			if req_name in reassign_req_names and not active_reassignment_request:
				active_reassignment_request = serialize_cr(
					frappe.get_doc("Grievance Change Request", req_name)
				)

	from oan_grievance_service.api.v1.administrative_area import (
		format_administrative_location,
		get_administrative_hierarchy,
	)

	area_hierarchy = get_administrative_hierarchy(doc.administrative_area)
	location_str = format_administrative_location(area_hierarchy)

	return success_response(
		data={
			"name": doc.name,
			"ticket_number": tn.display(doc.ticket_number),
			"status": doc.status,
			"escalated": bool(doc.escalated),
			"summary": {
				"description": doc.description,
				"desired_outcome": doc.desired_outcome,
				"service_category": doc.service_category,
				"grievance_type": doc.grievance_type,
				"administrative_area": doc.administrative_area,
				"administrative_hierarchy": area_hierarchy,
				"location": location_str,
				"administrative_unit": doc.administrative_unit,
				"submission_channel": doc.submission_channel,
			},
			"submitter": {
				"name": masked_name,
				"mobile": contact["contact_mobile"],
				"contact_mobile": contact["contact_mobile"],
				"country_code": contact["country_code"],
				"phone_number": contact["phone_number"],
				"email": contact["contact_email"],
				"contact_email": contact["contact_email"],
				"submitter_type": doc.submitter_type,
				"is_anonymous": bool(doc.is_anonymous),
				"assisted_by_officer": doc.assisted_by_officer,
			},
			"sla": {
				"sla_days": doc.sla_days,
				"sla_start_at": doc.sla_start_at,
				"sla_due_date": doc.sla_due_date,
				"sla_consumed_percent": sla.consumed_percent(doc),
				"next_escalation_at": doc.next_escalation_at,
				# Kept under its old name for existing clients: the confirmation window is
				# the Resolved state's timer.
				"confirmation_deadline": doc.state_deadline
				if doc.workflow_state == C.STATE_RESOLVED
				else None,
				"active_deferral_request": active_deferral_request,
			},
			"assignment": {
				"department": doc.assigned_dept,
				"assigned_to": doc.assigned_to,
				"routed_automatically": bool(doc.routed_automatically),
				"active_reassignment_request": active_reassignment_request,
			},
			"available_actions": _get_available_actions_for_user(doc),
			"can_request_more_info": contact["can_request_more_info"],
			"attachments": attachments,
			"timeline": entries,
			"has_more": has_more,
			"next_cursor": next_cursor,
		},
		message=_("Timeline retrieved successfully"),
	)


@route(
	"/<ticket_number>/message",
	methods=("POST",),
	summary="Post a message or an internal note, with optional files",
)
@frappe.whitelist()
@validate_request(PostMessageRequest)
@handle_api_errors
@require_role(ALLOWED_GRIEVANCE_ROLES)
def message(ticket_number: str, body: str, is_internal: bool | str | None = None, **kwargs):
	"""Post to the case thread: a public message, or an internal note when staff send
	`is_internal`. Files sent as multipart attach to the new entry.

	Never moves the case. A reply to an information request is the `Submitter Reply`
	action and an information request is the `Request More Info` action, both on /action.
	"""
	from oan_grievance_service.api.v1.attachment import prepare_uploads, store_uploads

	internal = bool(_parse_flag(is_internal))
	if is_internal is None and kwargs.get("type") in ("note", "internal", "internal_note"):
		internal = True
	doc = _load(ticket_number, ptype="write")
	user = frappe.session.user
	is_staff = permissions.is_staff(user)
	if internal and not is_staff:
		frappe.throw(_("Only staff members can post internal notes."), frappe.PermissionError)

	prepared = prepare_uploads(doc.name)
	entry = GrievanceTimeline.record(
		grievance=doc.name,
		entry_type="note" if internal else "message",
		is_internal=internal,
		body=body,
		author_user=user if is_staff else None,
		author_submitter=doc.submitter if not is_staff else None,
	)
	attachments = store_uploads(doc, prepared, timeline_entry=entry.name) if prepared else []

	return success_response(
		data={
			"name": entry.name,
			"ticket_number": tn.display(doc.ticket_number),
			"entry_type": entry.entry_type,
			"is_internal": bool(entry.is_internal),
			"author_type": "officer" if is_staff else "submitter",
			"created_on": entry.created_on,
			"status": doc.status,
			"attachments": attachments,
		},
		message=_("Note added successfully") if internal else _("Message posted successfully"),
	)


def add_note(ticket_number: str, body: str, **kwargs):
	"""Convenience wrapper for posting an internal note to a grievance thread."""
	return message(ticket_number=ticket_number, body=body, is_internal=True, **kwargs)


def _parse_flag(value):
	"""A boolean from a JSON body or a form field. The string "false" is truthy in
	Python, so a form-encoded flag has to be read, not tested."""
	if value is None:
		return None
	if isinstance(value, str):
		return value.strip().lower() not in ("", "0", "false", "no", "off")
	return bool(value)


def _change_response(doc, req, applied_message, pending_message, **extra):
	"""The answer every change-request-backed endpoint gives: the case as it now
	stands, and the request that holds the change."""
	from oan_grievance_service.api.v1.change_request import serialize

	doc.reload()
	current_state = _current_state(doc)
	timeline_entry = req.flags.timeline_entry
	return success_response(
		data={
			"ticket_number": tn.display(doc.ticket_number),
			"status": doc.status,
			"change_request": serialize(req),
			"current_state": current_state,
			"timeline_event": (
				_format_timeline_event(timeline_entry, doc, doc.status, doc.status)
				if timeline_entry
				else None
			),
			**{k: doc.get(k) for k in extra.get("fields", ())},
		},
		message=applied_message if req.status == "Approved" else pending_message,
	)


@route("/<ticket_number>/reassign", methods=("POST",), summary="Reassign grievance department and officer")
@frappe.whitelist()
@validate_request(ReassignGrievanceRequest)
@handle_api_errors
@require_role(STAFF_ROLES)
def reassign(
	ticket_number: str,
	target_department: str,
	target_officer: str | None = None,
	target_category: str | None = None,
	target_grievance_type: str | None = None,
	reason: str | None = None,
	**kwargs,
):
	"""Request a new department and/or officer for the case.

	Applied at once for the target department head or an admin; otherwise it waits
	for the receiving department's head and the case does not move.
	"""
	from oan_grievance_service.api.v1.change_request import raise_change_request
	from oan_grievance_service.services import reassignment

	doc = _load(ticket_number, ptype="write")
	changes = reassignment.resolve(
		doc,
		target_department,
		target_officer,
		frappe.session.user,
		category=target_category,
		grievance_type=target_grievance_type,
	)

	req = raise_change_request(doc, _("Reassignment"), changes, reason=reason)
	return _change_response(
		doc,
		req,
		_("Grievance reassigned successfully"),
		_("Reassignment requested; awaiting approval"),
		fields=(
			"assigned_dept",
			"assigned_to",
			"service_category",
			"grievance_type",
			"sla_due_date",
		),
	)


@route("/<ticket_number>/defer-sla", methods=("POST",), summary="Extend SLA deadline by deferral")
@frappe.whitelist()
@validate_request(DeferSLARequest)
@handle_api_errors
@require_role(STAFF_ROLES)
def defer_sla(
	ticket_number: str,
	additional_days: int,
	reason: str,
	**kwargs,
):
	"""Request the SLA deadline be pushed out by `additional_days`.

	A shorthand for a change request on `sla_due_date`, decided up the hierarchy
	unless the deferral policy lets officers defer without a supervisor.
	"""
	from frappe.utils import add_days, get_datetime

	from oan_grievance_service.api.v1.change_request import raise_change_request

	doc = _load(ticket_number, ptype="write")
	if not doc.sla_due_date:
		frappe.throw(_("This grievance has no SLA deadline to defer."), title=_("No SLA Deadline"))

	days = int(additional_days)
	new_due = add_days(get_datetime(doc.sla_due_date), days)
	req = raise_change_request(
		doc, _("SLA deferral: +{0} days").format(days), {"sla_due_date": str(new_due)}, reason=reason
	)
	return _change_response(
		doc,
		req,
		_("SLA deadline extended successfully"),
		_("Deferral requested; awaiting approval"),
		fields=("sla_due_date",),
	)


@route(
	"/<ticket_number>/response-templates",
	methods=("GET",),
	summary="Response templates for a workflow action, filled in for this grievance",
)
@frappe.whitelist()
@validate_request(ResponseTemplatesRequest)
@handle_api_errors
@require_role(STAFF_ROLES)
def response_templates(ticket_number: str, action: str, service_category: str | None = None, **kwargs):
	"""Active templates for workflow `action` that fit the case's department and category,
	most specific first, rendered with the case's details for the officer to edit."""
	from oan_grievance_service.grievance_masters.doctype.grievance_response_template.grievance_response_template import (
		render,
		templates_for,
	)

	doc = _load(ticket_number, ptype="write")
	category = service_category or doc.service_category

	items = []
	for row in templates_for(doc, action, service_category=category):
		rendered = render(row, doc, "body")
		items.append(
			{
				"template": row.name,
				"title": row.title,
				"action": row.workflow_action,
				"workflow_action": row.workflow_action,
				"department": row.department,
				"service_category": row.service_category,
				"reason": rendered,
				"reason_parts": response_body.split(rendered),
				"note": render(row, doc, "note") if row.get("note") else None,
			}
		)
	return success_response(data={"items": items}, message=_("Response templates fetched successfully"))


def _load(ticket_number, ptype="read"):
	"""Fetch a grievance by ticket number as the submitter typed it.

	Normalised first: the number is printed grouped (3-001-002A-0) and read back
	over a phone line, so the hyphens, casing and the O/I/L substitutions the
	alphabet anticipates must not decide whether a farmer can reach their own
	case.
	"""
	normalized = tn.normalize(ticket_number)
	name = frappe.db.get_value("Grievance", {"ticket_number": normalized}, "name")
	if not name:
		if frappe.db.exists("Grievance", normalized):
			name = normalized
		elif frappe.db.exists("Grievance", ticket_number):
			name = ticket_number
		else:
			frappe.throw(_("No grievance found with that ticket number."), title=_("Not Found"))
	doc = frappe.get_doc("Grievance", name)
	from oan_grievance_service.permissions import has_grievance_permission

	if not has_grievance_permission(doc, ptype):
		frappe.throw(
			_("Not permitted to access this grievance."), frappe.PermissionError, title=_("Forbidden")
		)
	return doc


@route("/summary", methods=("GET",), summary="KPI cards summarising grievance status")
@frappe.whitelist()
@handle_api_errors
@require_role(ALLOWED_GRIEVANCE_ROLES)
def summary():
	"""Counts of visible grievances on each queue status card.

	Draft is excluded. The cards are All, In Progress, Require More Info, Rejected,
	Resolved and Closed. Officers also get an Assigned card. Any other workflow
	state is counted under In Progress. Each card includes its display order and
	whether the workflow treats it as terminal (no per-card default).
	"""
	return success_response(
		data={"cards": get_status_summary()},
		message=_("Grievance status summary fetched successfully"),
	)


@route("/options", methods=("GET",), summary="Get grievance options and dropdowns")
@frappe.whitelist()
@handle_api_errors
@require_role(ALLOWED_GRIEVANCE_ROLES)
def options(
	service_category: str | None = None,
	category: str | None = None,
	department: str | None = None,
	administrative_area: str | None = None,
	**kwargs,
):
	"""Management and lookup options for submitters, grievance officers and admins.

	Returns reference lists for case filing, management, triage, and filtering,
	including departments, lifecycle statuses, categories, and types.
	When `department` is provided and the caller is staff, cascades and includes
	active officers under that department (optionally narrowed by `service_category`
	and `administrative_area`). The caller is never listed. Submitters never receive officers.

	Args:
	    service_category (str, optional): Filter grievance types & officers by service category (e.g. 'Inputs').
	    category (str, optional): Alias for service_category.
	    department (str, optional): Department ID to fetch assigned officers.
	    administrative_area (str, optional): Administrative area to scope officer assignments.

	Returns:
	    departments: Active grievance departments
	    statuses: Grievance lifecycle statuses with metadata
	    service_categories: Active service categories
	    grievance_types: Active grievance types (optionally filtered by service_category)
	    submission_channels: Active intake channels
	    officers: Active officers under the specified department (staff only, present only if `department` is passed)
	"""
	cat = service_category or category
	service_categories = get_service_categories()
	grievance_types = get_grievance_types(service_category=cat)

	data = {
		"departments": get_departments(),
		"statuses": get_status_options(),
		"service_categories": service_categories,
		"grievance_types": grievance_types,
		"submission_channels": active_channels(),
	}

	if department and permissions.is_staff():
		data["officers"] = get_department_officers(
			department=department,
			service_category=cat,
			administrative_area=administrative_area,
			exclude_user=frappe.session.user,
		)

	return success_response(data=data, message=_("Grievance options fetched successfully"))
