#!/usr/bin/env python3
"""
generate_openapi_spec.py

Dynamically discovers all declared REST routes across oan_grievance_service,
introspects request/response models, query parameters, security requirements,
and builds openapi_v1.yaml and openapi_v1.public.yaml.

Outputs:
  - openapi_v1.yaml: Engineering/Internal specification with vendor extensions
    (x-legacy-rpc-method, x-schema-confidence).
  - openapi_v1.public.yaml: Public/Gateway contract with vendor extensions stripped.

Usage:
  python3 openapi/generate_openapi_spec.py
"""

import copy
import importlib
import inspect
import re
import sys
from pathlib import Path
from typing import Any

import frappe
from oan_auth_service.api.router import _exempt_paths, _rules

try:
	from oan_auth_service.openapi_spec import (
		ARR,
		BINARY,
		OBJ,
		REF,
		B,
		I,
		N,
		S,
		dump_spec,
		make_envelope,
		query_parameters,
		request_model,
		request_schema,
		strip_extensions,
	)
except ImportError:
	import yaml
	from pydantic import BaseModel

	def S(**kw: Any) -> dict[str, Any]:
		return {"type": "string", **kw}

	def I(**kw: Any) -> dict[str, Any]:  # noqa: E743
		return {"type": "integer", **kw}

	def N(**kw: Any) -> dict[str, Any]:
		return {"type": "number", **kw}

	def B(**kw: Any) -> dict[str, Any]:
		return {"type": "boolean", **kw}

	def ARR(items: Any, **kw: Any) -> dict[str, Any]:
		return {"type": "array", "items": items, **kw}

	def OBJ(
		props: dict[str, Any],
		required: list[str] | None = None,
		description: str | None = None,
		confidence: str | None = None,
		**kw: Any,
	) -> dict[str, Any]:
		d: dict[str, Any] = {"type": "object", "properties": props, **kw}
		if required:
			d["required"] = required
		if description:
			d["description"] = description
		if confidence:
			d["x-schema-confidence"] = confidence
		return d

	def REF(name: str) -> dict[str, str]:
		return {"$ref": f"#/components/schemas/{name}"}

	BINARY = {"type": "string", "format": "binary"}

	def make_envelope(
		data_ref: str,
		is_list: bool = False,
		nullable_data: bool = False,
		description: str = "Successful response",
	) -> dict[str, Any]:
		if is_list:
			data_prop: Any = ARR(REF(data_ref))
		elif nullable_data:
			data_prop = {**REF(data_ref), "nullable": True}
		else:
			data_prop = REF(data_ref)
		return OBJ(
			{
				"status": S(example="success", enum=["success"]),
				"message": S(nullable=True, description="Optional response message"),
				"data": data_prop,
				"meta": REF("ApiMeta"),
				"request_id": S(format="uuid", nullable=True, description="Tracing correlation ID"),
			},
			required=["status", "data"],
			description=description,
		)

	_DROP_KEYWORDS = {"title"}

	def oas30(node: Any) -> Any:
		if isinstance(node, list):
			return [oas30(n) for n in node]
		if not isinstance(node, dict):
			return node
		out: dict[str, Any] = {}
		for key, value in node.items():
			if key in _DROP_KEYWORDS:
				continue
			if key == "properties":
				out[key] = {name: oas30(prop) for name, prop in value.items()}
			else:
				out[key] = oas30(value)
		for key in ("anyOf", "oneOf"):
			options = out.get(key)
			if options and any(o.get("type") == "null" for o in options):
				rest = [o for o in options if o.get("type") != "null"]
				del out[key]
				out = {**rest[0], **out} if len(rest) == 1 else {key: rest, **out}
				out["nullable"] = True
		if "const" in out:
			out["enum"] = [out.pop("const")]
		if "default" in out and out["default"] is None and not out.get("nullable"):
			del out["default"]
		if "$ref" in out and len(out) > 1:
			out = {"allOf": [{"$ref": out.pop("$ref")}], **out}
		return out

	def request_schema(
		cls: type[BaseModel], path_params: list[str], components: dict[str, Any]
	) -> dict[str, Any] | None:
		schema = cls.model_json_schema(ref_template="#/components/schemas/{model}")
		for name, sub in schema.pop("$defs", {}).items():
			components.setdefault(name, oas30(sub))
		schema = oas30(schema)
		props = {k: v for k, v in schema.get("properties", {}).items() if k not in path_params}
		if not props:
			return None
		schema["properties"] = props
		required = [k for k in schema.get("required", []) if k in props]
		if required:
			schema["required"] = required
		else:
			schema.pop("required", None)
		if cls.__doc__ and "description" not in schema:
			schema["description"] = inspect.cleandoc(cls.__doc__)
		return schema

	def query_parameters(schema: dict[str, Any]) -> list[dict[str, Any]]:
		required = set(schema.get("required", []))
		params = []
		for name, prop in schema["properties"].items():
			param: dict[str, Any] = {
				"name": name,
				"in": "query",
				"required": name in required,
				"schema": {k: v for k, v in prop.items() if k != "description"},
			}
			if prop.get("description"):
				param["description"] = prop["description"]
			params.append(param)
		return params

	def request_model(endpoint_fn: Any) -> type[BaseModel] | None:
		return getattr(endpoint_fn, "_request_schema", None) or getattr(
			inspect.unwrap(endpoint_fn), "_request_schema", None
		)

	def strip_extensions(o: Any) -> Any:
		if isinstance(o, dict):
			return {k: strip_extensions(v) for k, v in o.items() if not k.startswith("x-")}
		if isinstance(o, list):
			return [strip_extensions(v) for v in o]
		return o

	def dump_spec(doc: dict[str, Any], path, header: list[str]) -> None:
		with open(path, "w", encoding="utf-8") as f:  # nosemgrep: frappe-security-file-traversal
			for line in header:
				f.write(f"# {line}\n")
			yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False, width=100, allow_unicode=True)


from werkzeug.routing import Rule

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
INTERNAL_SPEC_OUTPUT = SCRIPT_DIR / "openapi_v1.yaml"
PUBLIC_SPEC_OUTPUT = SCRIPT_DIR / "openapi_v1.public.yaml"


# ---------------------------------------------------------------------------
# Components: Data Schemas
# ---------------------------------------------------------------------------
DATA_SCHEMAS: dict[str, Any] = {}


def data(name: str, schema: dict[str, Any]) -> str:
	DATA_SCHEMAS[name] = schema
	return name


# Standard Envelopes & Metadata
data(
	"ApiMeta",
	OBJ(
		{
			"api_version": S(example="v1", description="Semantic API version"),
			"status": S(example="current", description="Lifecycle status"),
		},
		required=["api_version"],
	),
)

data(
	"StandardErrorResponse",
	OBJ(
		{
			"status": S(example="error", enum=["error"]),
			"message": S(description="Human-readable error description"),
			"exception": S(nullable=True, description="Exception class name"),
			"errors": ARR(
				OBJ(
					{
						"field": S(nullable=True, description="Field causing the validation error"),
						"message": S(description="Error message for the specific field"),
					}
				),
				nullable=True,
				description="Structured validation errors if applicable",
			),
			"meta": REF("ApiMeta"),
			"request_id": S(format="uuid", nullable=True, description="Tracing correlation ID"),
		},
		required=["status", "message"],
		description="Standard error envelope returned on 4xx/5xx responses",
	),
)

data(
	"PaginationMeta",
	OBJ(
		{
			"page": I(example=1, description="Current page number"),
			"page_size": I(example=20, description="Items per page"),
			"total_count": I(example=142, description="Total matching records"),
			"total_pages": I(example=8, description="Total pages available"),
			"has_next": B(description="True when a later page exists"),
			"has_prev": B(description="True when an earlier page exists"),
		},
		required=["page", "page_size", "total_count", "total_pages", "has_next", "has_prev"],
		description="Pagination metadata block",
	),
)

# Health & Ping
data(
	"HealthData",
	OBJ(
		{
			"status": S(example="healthy"),
			"service": S(example="oan_grievance_service"),
			"api_version": S(example="v1"),
		},
		required=["status", "service", "api_version"],
		description="Service health status payload",
	),
)

data(
	"PingData",
	OBJ(
		{
			"ping": S(example="pong"),
			"service": S(example="oan_grievance_service"),
			"api_version": S(example="v1"),
		},
		required=["ping", "service", "api_version"],
		description="Service ping payload",
	),
)

# Submitter Options & Profile
data(
	"SubmitterTypeItem",
	OBJ(
		{
			"type_name": S(example="Individual Farmer"),
			"code": S(example="IND_FARMER"),
			"description": S(nullable=True),
		},
		required=["type_name"],
	),
)

data(
	"SubmissionTypeItem",
	OBJ(
		{
			"type_name": S(example="Mobile App"),
			"code": S(example="MOB_APP"),
			"description": S(nullable=True),
		},
		required=["type_name"],
	),
)

data(
	"OptionKeyValue",
	OBJ(
		{
			"value": S(example="Inputs"),
			"label": S(example="Agricultural Inputs"),
			"code": S(nullable=True),
		},
		required=["value", "label"],
	),
)

data(
	"PhoneExtensionItem",
	OBJ(
		{
			"country": S(example="Ethiopia"),
			"code": S(example="ET"),
			"isd": S(example="+251"),
		},
		required=["country", "code", "isd"],
	),
)

data(
	"SubmitterOptionsData",
	OBJ(
		{
			"submitter_types": ARR(REF("SubmitterTypeItem")),
			"submission_types": ARR(REF("SubmissionTypeItem")),
			"preferred_languages": ARR(OBJ({"code": S(), "label": S()}, required=["code", "label"])),
			"service_categories": ARR(REF("OptionKeyValue")),
			"grievance_types": ARR(REF("OptionKeyValue")),
			"phone_extensions": ARR(REF("PhoneExtensionItem"), nullable=True),
		},
		required=[
			"submitter_types",
			"submission_types",
			"preferred_languages",
			"service_categories",
			"grievance_types",
		],
		description="Public dropdown options and intake reference data",
	),
)

data(
	"SubmitterProfileData",
	OBJ(
		{
			"profile_id": S(description="Unique Grievance Submitter Profile document name"),
			"full_name": S(description="Full name of submitter or representative"),
			"type": S(description="Submitter Type e.g. Individual Farmer or Cooperative"),
			"role": S(example="Grievance Submitter"),
			"identity_scheme": S(nullable=True, enum=["fayda", "org", "phone", None]),
			"identity_value": S(nullable=True),
			"fayda_id": S(nullable=True),
			"registration_number": S(nullable=True),
			"contact_mobile": S(nullable=True),
			"country_code": S(example="+251", nullable=True),
			"phone_number": S(example="911887766", nullable=True),
			"contact_email": S(format="email", nullable=True),
			"preferred_language": S(example="en", nullable=True),
			"administrative_area": S(nullable=True),
			"administrative_unit": S(nullable=True),
			"active": I(enum=[0, 1]),
			"is_blocked": I(enum=[0, 1]),
		},
		required=["profile_id", "full_name", "type", "role"],
		description="Grievance submitter profile details",
	),
)

data(
	"SubmitterIdentityItem",
	OBJ(
		{"scheme": S(example="phone"), "value": S(example="+251911887766")},
		required=["scheme", "value"],
		description="Submitter deduplication identity key",
	),
)

data(
	"SubmitterRegisterResultData",
	OBJ(
		{
			"profile_id": S(description="Unique Grievance Submitter Profile document name"),
			"submitter_type": S(example="Individual Farmer"),
			"submitter_name": S(example="Abebe Bikila", nullable=True),
			"contact_mobile": S(example="+251911887766", nullable=True),
			"country_code": S(example="+251", nullable=True),
			"phone_number": S(example="911887766", nullable=True),
			"contact_email": S(format="email", nullable=True),
			"dedupe_key": S(nullable=True),
			"identities": ARR(REF("SubmitterIdentityItem")),
			"administrative_area": S(nullable=True),
			"administrative_unit": S(nullable=True),
			"active": B(),
			"is_blocked": B(),
			"blocked_reason": S(nullable=True),
		},
		required=["profile_id", "submitter_type", "active", "is_blocked"],
		description="Outcome of submitter profile registration",
	),
)

data(
	"SubmitterBlockResultData",
	OBJ(
		{
			"profile_id": S(description="Submitter profile ID"),
			"is_blocked": B(description="Whether the submitter profile is blocked"),
			"blocked_reason": S(nullable=True, description="Reason for blocking"),
			"active": B(description="Whether the submitter profile is active"),
		},
		required=["profile_id", "is_blocked", "active"],
		description="Outcome of submitter block or unblock operation",
	),
)

# Administrative Areas
data(
	"AdministrativeAreaItem",
	OBJ(
		{
			"area_id": S(example="region-ET14", description="Canonical area ID"),
			"area_name": S(example="Oromia"),
			"code": S(example="ET14", nullable=True),
			"path_code": S(example="ET.ET14", nullable=True),
			"level_name": S(
				example="Region", description="Administrative tier (e.g. Region, Zone, Woreda, Kebele)"
			),
			"parent_administrative_area": S(nullable=True),
			"is_group": I(enum=[0, 1]),
			"depth": I(example=1),
		},
		required=["area_id", "area_name", "level_name"],
		description="Administrative area hierarchy node",
	),
)

data(
	"AdministrativeAreasListData",
	OBJ(
		{
			"areas": ARR(REF("AdministrativeAreaItem")),
			"count": I(example=12),
			"parent": S(nullable=True),
			"level_name": S(nullable=True),
		},
		required=["areas", "count"],
		description="List of administrative area nodes for cascading dropdowns or search",
	),
)

data(
	"BreadcrumbItem",
	OBJ(
		{
			"area_id": S(),
			"area_name": S(),
			"code": S(nullable=True),
			"path_code": S(nullable=True),
			"level_name": S(),
			"depth": I(),
		},
		required=["area_id", "area_name", "level_name"],
	),
)

data(
	"AreaAncestorsData",
	OBJ(
		{
			"current": REF("AdministrativeAreaItem"),
			"breadcrumbs": ARR(REF("BreadcrumbItem")),
		},
		required=["breadcrumbs"],
		description="Ancestor hierarchy breadcrumbs from country root to the node",
	),
)

# Grievance Drafts
data(
	"DraftData",
	OBJ(
		{
			"name": S(description="Draft document name"),
			"ticket_number": S(nullable=True, description="Assigned ticket number if submitted"),
			"client_submission_uuid": S(description="Stable client-generated draft key"),
			"status": S(example="Draft"),
			"workflow_state": S(example="Draft"),
			"submission_channel": S(nullable=True),
			"submitter_type": S(nullable=True),
			"submitter_name": S(nullable=True),
			"contact_mobile": S(nullable=True),
			"country_code": S(example="+251", nullable=True),
			"phone_number": S(example="911887766", nullable=True),
			"phone": S(nullable=True),
			"contact_email": S(nullable=True),
			"administrative_area": S(nullable=True),
			"administrative_unit": S(nullable=True),
			"service_category": S(nullable=True),
			"grievance_type": S(nullable=True),
			"associated_service_provider": S(nullable=True),
			"description": S(nullable=True),
			"desired_outcome": S(nullable=True),
			"is_anonymous": I(enum=[0, 1]),
			"attachments": ARR(OBJ({})),
			"attachment_count": I(),
			"owner": S(nullable=True),
		},
		required=["client_submission_uuid", "status", "workflow_state"],
		description="Draft grievance state",
	),
)

data(
	"DraftDiscardData",
	OBJ(
		{
			"discarded": B(description="Whether the draft was successfully discarded"),
		},
		required=["discarded"],
		description="Outcome of draft discard operation",
	),
)

# Grievance Core & Lifecycle
data(
	"GrievanceSubmitResultData",
	OBJ(
		{
			"ticket_number": S(example="ET14IN000012026", description="Formatted ticket number"),
			"status": S(example="Submitted"),
			"acknowledgement_status": S(example="Sent", nullable=True),
			"assigned_officer": S(nullable=True),
			"sla_target_date": S(format="date-time", nullable=True),
			"creation": S(format="date-time"),
			"is_anonymous": I(enum=[0, 1], example=0),
			"workflow_state": S(nullable=True),
			"client_submission_uuid": S(nullable=True),
			"routing_rule": S(nullable=True),
		},
		required=["ticket_number", "status"],
		description="Acknowledgement outcome and ticket identifier returned on submission",
	),
)

data(
	"GrievanceListItem",
	OBJ(
		{
			"name": S(description="Internal document ID"),
			"ticket_number": S(example="3001002A0"),
			"ticket_number_display": S(
				example="3-001-002A-0", nullable=True, description="Grouped ticket number for human reading"
			),
			"status": S(
				example="Submitted",
				description="Public workflow status (Submitted, Under Investigation, Require More Info, Resolved, Closed, Reopened, Rejected)",
			),
			"service_category": S(example="Inputs"),
			"grievance_type": S(example="Fertilizer Shortage"),
			"administrative_area": S(example="kebele-ET140108101008"),
			"administrative_unit": S(nullable=True),
			"submitter_name": S(example="Abebe Bikila"),
			"contact_mobile": S(example="+251911887766", nullable=True),
			"country_code": S(example="+251", nullable=True),
			"phone_number": S(example="911887766", nullable=True),
			"contact_email": S(nullable=True),
			"assigned_officer": S(nullable=True),
			"sla_target_date": S(format="date-time", nullable=True),
			"is_escalated": I(enum=[0, 1]),
			"escalated": B(nullable=True),
			"is_anonymous": B(nullable=True),
			"confirmation_deadline": S(format="date-time", nullable=True),
			"creation": S(format="date-time"),
			"modified": S(format="date-time"),
		},
		required=["ticket_number", "status", "service_category", "grievance_type", "creation"],
		description="Summary record of a grievance in list view",
	),
)

data(
	"GrievanceListData",
	OBJ(
		{
			"grievances": ARR(REF("GrievanceListItem")),
			"pagination": REF("PaginationMeta"),
		},
		required=["grievances", "pagination"],
		description="Filtered and paginated list of grievances",
	),
)

data(
	"GrievanceDetailData",
	OBJ(
		{
			"ticket_number": S(example="3001002A0"),
			"ticket_number_display": S(
				example="3-001-002A-0", nullable=True, description="Grouped ticket number for human reading"
			),
			"name": S(),
			"status": S(),
			"service_category": S(),
			"grievance_type": S(),
			"description": S(),
			"submission_channel": S(),
			"preferred_language": S(nullable=True),
			"administrative_area": S(),
			"administrative_unit": S(nullable=True),
			"submitter": S(nullable=True),
			"submitter_name": S(),
			"contact_mobile": S(nullable=True),
			"country_code": S(example="+251", nullable=True),
			"phone_number": S(example="911887766", nullable=True),
			"contact_email": S(nullable=True),
			"is_anonymous": I(enum=[0, 1]),
			"assigned_officer": S(nullable=True),
			"assisted_by_officer": S(nullable=True),
			"sla_target_date": S(format="date-time", nullable=True),
			"sla_status": S(nullable=True),
			"resolution_details": S(nullable=True),
			"satisfaction_rating": I(nullable=True),
			"reopen_count": I(example=0),
			"is_escalated": I(enum=[0, 1]),
			"confirmation_deadline": S(format="date-time", nullable=True),
			"creation": S(format="date-time"),
			"modified": S(format="date-time"),
		},
		required=["ticket_number", "status", "description", "creation"],
		description="Full grievance case details",
	),
)

data(
	"ResponseParts",
	OBJ(
		{
			"action_taken": S(description="What the department did"),
			"resolution_summary": S(description="The outcome for the submitter"),
		},
		required=["action_taken", "resolution_summary"],
		description="A department response's two parts, split from the stored text. Null on any text "
		"that was not written in two parts (a rejection, a reopen, an older template).",
	),
)

data(
	"TimelineEventItem",
	OBJ(
		{
			"name": S(nullable=True, description="Timeline entry identifier"),
			"entry_type": S(
				example="status_change",
				description="Event classification (status_change, note, message, attachment)",
			),
			"from_status": S(nullable=True),
			"to_status": S(nullable=True),
			"author_role": S(nullable=True, description="Role of the actor e.g. Woreda Officer or Submitter"),
			"author_type": S(nullable=True, enum=["submitter", "officer", "system"]),
			"body": S(nullable=True, description="Timeline message or description text"),
			"body_parts": {**REF("ResponseParts"), "nullable": True},
			"is_internal": B(description="Whether visible only to staff"),
			"action": S(nullable=True, description="Workflow action behind a status change entry"),
			"attachments": ARR(REF("AttachmentItem"), description="Files attached to this entry"),
			"created_on": S(format="date-time", nullable=True),
			"creation": S(format="date-time", nullable=True),
		},
		required=["entry_type"],
		description="Audit and communication event on the grievance timeline",
	),
)

data(
	"TimelineSubmitterDetail",
	OBJ(
		{
			"name": S(nullable=True),
			"mobile": S(nullable=True),
			"contact_mobile": S(nullable=True),
			"country_code": S(nullable=True),
			"phone_number": S(nullable=True),
			"email": S(nullable=True),
			"contact_email": S(nullable=True),
			"submitter_type": S(nullable=True),
			"is_anonymous": B(),
			"assisted_by_officer": S(nullable=True),
		},
		required=["is_anonymous"],
		description="Submitter contact and identity details on the timeline",
	),
)

data(
	"GrievanceTimelineData",
	OBJ(
		{
			"ticket_number": S(example="ET14IN000012026"),
			"status": S(example="Under Investigation"),
			"escalated": B(),
			"summary": OBJ({"description": S(nullable=True), "desired_outcome": S(nullable=True)}),
			"sla": OBJ(
				{
					"sla_due_date": S(format="date-time", nullable=True),
					"active_deferral_request": {**REF("ChangeRequestData"), "nullable": True},
				}
			),
			"assignment": OBJ(
				{
					"department": S(nullable=True),
					"assigned_to": S(nullable=True),
					"routed_automatically": B(),
					"active_reassignment_request": {**REF("ChangeRequestData"), "nullable": True},
				}
			),
			"submitter": REF("TimelineSubmitterDetail"),
			"timeline": ARR(REF("TimelineEventItem")),
		},
		required=["ticket_number", "status", "timeline"],
		description="Chronological event log and message history",
	),
)

data(
	"GrievanceActionResultData",
	OBJ(
		{
			"ticket_number": S(example="3-001-002A-0"),
			"status": S(example="Resolved"),
			"action": S(description="Workflow action taken"),
			"attachments": ARR(REF("AttachmentItem"), description="Files stored with the action"),
			"current_state": REF("GrievanceCurrentState"),
			"timeline_event": {**REF("TimelineEventItem"), "nullable": True},
			"available_actions": ARR(REF("AvailableActionItem")),
		},
		required=["ticket_number", "status", "action", "current_state", "available_actions"],
		description="Outcome of a workflow action on a grievance",
	),
)

data(
	"GrievanceMessageData",
	OBJ(
		{
			"name": S(description="Timeline entry ID"),
			"ticket_number": S(example="3-001-002A-0"),
			"entry_type": S(enum=["message", "note"]),
			"is_internal": B(),
			"author_type": S(enum=["officer", "submitter"]),
			"created_on": S(format="date-time", nullable=True),
			"status": S(description="Grievance status, unchanged by a message"),
			"attachments": ARR(REF("AttachmentItem"), description="Files stored with the message"),
		},
		required=["name", "ticket_number", "entry_type", "is_internal", "status"],
		description="The timeline entry a message or internal note created",
	),
)

data(
	"GrievanceOptionsData",
	OBJ(
		{
			"statuses": ARR(
				OBJ(
					{
						"status": S(),
						"label": S(),
						"order": I(description="Display order of the queue status"),
						"is_open": I(),
						"is_terminal": I(
							description="1 when every mapped Frappe workflow state is terminal; 0 when absent"
						),
					}
				)
			),
			"departments": ARR(
				OBJ({"department_id": S(), "department_name": S()}, additionalProperties=True)
			),
			"service_categories": ARR(REF("OptionKeyValue")),
			"grievance_types": ARR(REF("OptionKeyValue")),
			"submission_channels": ARR(S()),
			"officers": ARR(
				OBJ(
					{
						"user_id": S(description="Officer user ID"),
						"full_name": S(description="Officer display name"),
						"role_level": S(description="Grievance role level code"),
						"is_primary": B(description="Whether officer is designated primary"),
					}
				),
				description="Active officers under the specified department. Staff only; present only when department is supplied.",
			),
		},
		required=["statuses", "departments", "service_categories", "grievance_types", "submission_channels"],
		description="Grievance management options and active dropdown choices for staff",
	),
)

data(
	"StatusCard",
	OBJ(
		{
			"status": S(example="In Progress"),
			"label": S(example="In Progress"),
			"order": I(description="Display order of the queue status", example=2),
			"is_open": I(enum=[0, 1], example=1),
			"is_terminal": I(
				description="1 when every mapped Frappe workflow state is terminal. Absent workflow states are non-terminal.",
				enum=[0, 1],
				example=0,
			),
			"count": I(
				description="Grievances on this card visible to the caller. Omitted on the options list.",
				example=12,
			),
		},
		required=["status", "label", "order", "is_open", "is_terminal"],
		description="One queue status for the all-grievances KPI cards",
	),
)

data(
	"GrievanceStatusSummaryData",
	OBJ(
		{"cards": ARR(REF("StatusCard"))},
		required=["cards"],
		description="Status summary for the all-grievances queue. Draft is excluded.",
	),
)

# Department responses
data(
	"ResponseTemplateItem",
	OBJ(
		{
			"template": S(description="Template ID, sent as `template` to /action"),
			"title": S(),
			"action": S(nullable=True, description="Workflow action the template is for"),
			"workflow_action": S(nullable=True, description="Workflow action the template is for"),
			"department": S(nullable=True, description="Department the template is scoped to; null for all"),
			"service_category": S(
				nullable=True, description="Service category the template is scoped to; null for all"
			),
			"reason": S(description="Template body rendered for the case, as plain text. Prefills `reason`."),
			"reason_parts": {**REF("ResponseParts"), "nullable": True},
			"note": S(
				nullable=True,
				description="Template note rendered for the case, as plain text. Prefills `note`.",
			),
		},
		required=["template", "title", "reason"],
		description="A response template filled in for the grievance",
	),
)

data(
	"ResponseTemplatesData",
	OBJ(
		{"items": ARR(REF("ResponseTemplateItem"), description="Most specific first")},
		required=["items"],
	),
)

# Attachments
data(
	"AttachmentItem",
	OBJ(
		{
			"attachment": S(description="Unique identifier of the attachment record"),
			"name": S(description="Document name (alias for attachment)", nullable=True),
			"file_name": S(description="Original filename"),
			"file_url": S(description="URL to the uploaded file", nullable=True),
			"mime_type": S(description="MIME type of the file"),
			"size_bytes": I(description="File size in bytes"),
			"checksum_sha256": S(description="SHA-256 checksum"),
			"scan_status": S(description="Antivirus scan status (Pending, Clean, Infected)"),
			"document_type": S(nullable=True, description="Classification of document"),
			"timeline_entry": S(nullable=True, description="Timeline entry the file is attached to"),
			"creation": S(format="date-time", nullable=True),
		},
		required=["file_name", "mime_type", "size_bytes"],
		description="Metadata for an uploaded evidence attachment",
	),
)

data(
	"AttachmentDownloadData",
	OBJ(
		{
			"file_name": S(description="Original filename"),
			"file_url": S(
				description="Frappe private-file URL; needs a Frappe session cookie, not a bearer token"
			),
			"view_url": S(
				description="API route that streams the bytes under the bearer token: "
				"/api/v1/attachments/{attachment_id}/view"
			),
			"mime_type": S(description="MIME type"),
			"size_bytes": I(description="Size in bytes"),
			"checksum_sha256": S(description="SHA-256 checksum"),
		},
		required=["file_name", "file_url", "view_url", "mime_type", "size_bytes"],
		description="Download metadata for a clean attachment",
	),
)

data(
	"DeleteAttachmentData",
	OBJ(
		{"deleted": B(description="True if attachment was successfully deleted")},
		required=["deleted"],
		description="Confirmation of attachment removal",
	),
)

# Change Requests
data(
	"ChangeItem",
	OBJ(
		{
			"fieldname": S(description="Grievance field to modify"),
			"old_value": S(nullable=True, description="Value before change"),
			"new_value": S(nullable=True, description="Requested value"),
		},
		required=["fieldname"],
		description="Specific field modification item in a change request",
	),
)

data(
	"ApprovalTrailItem",
	OBJ(
		{
			"action": S(example="Requested", description="Approval lifecycle action"),
			"user": S(description="User performing action"),
			"pending_with": S(nullable=True, description="User or role pending decision"),
			"at": S(format="date-time", nullable=True, description="Timestamp of action"),
			"note": S(nullable=True, description="Approval or rejection remarks"),
		},
		required=["action", "user"],
		description="Change request approval audit step",
	),
)

data(
	"ChangeRequestData",
	OBJ(
		{
			"name": S(description="Unique change request document ID"),
			"ticket_number": S(description="Associated grievance ticket number"),
			"subject": S(description="Summary of requested change"),
			"reason": S(nullable=True, description="Reason or justification"),
			"status": S(example="Pending", enum=["Pending", "Approved", "Rejected"]),
			"requested_by": S(description="User who raised the request"),
			"requested_at": S(format="date-time", nullable=True),
			"pending_with": S(nullable=True, description="User currently required to decide"),
			"pending_since": S(format="date-time", nullable=True),
			"decided_by": S(nullable=True, description="User who finalized decision"),
			"decided_at": S(format="date-time", nullable=True),
			"decision_note": S(nullable=True, description="Decision explanation"),
			"changes": ARR(REF("ChangeItem")),
			"trail": ARR(REF("ApprovalTrailItem")),
		},
		required=["name", "ticket_number", "subject", "status", "changes", "trail"],
		description="Change request snapshot and hierarchy approval status",
	),
)

data(
	"ChangeRequestListData",
	OBJ(
		{
			"items": ARR(REF("ChangeRequestData")),
			"count": I(example=3, description="Number of matching change requests"),
		},
		required=["items", "count"],
		description="List of change requests",
	),
)

data(
	"AvailableActionItem",
	OBJ(
		{
			"action": S(description="Action name"),
			"action_code": S(description="Stable machine-readable action identifier"),
			"label": S(description="Localized action label"),
			"requires_reason": B(description="Always true: every action carries a reason"),
			"requires_rating": B(description="True if the action accepts a rating (e.g. Close Case)"),
		},
		required=["action", "label", "requires_reason"],
	),
)

data(
	"GrievanceCurrentState",
	OBJ(
		{
			"status": S(description="Public grievance status"),
			"escalated": B(description="Whether case is currently escalated"),
			"assigned_to": S(nullable=True, description="Assigned officer email"),
			"department": S(nullable=True, description="Assigned department ID"),
			"updated_at": S(format="date-time", nullable=True),
			"available_actions": ARR(REF("AvailableActionItem")),
		},
		required=["status", "escalated", "available_actions"],
		description="Current case workflow status and permitted next actions",
	),
)

data(
	"GrievanceChangeResponseData",
	OBJ(
		{
			"ticket_number": S(example="ET14IN000012026", description="Grievance ticket number"),
			"status": S(example="Under Investigation"),
			"change_request": REF("ChangeRequestData"),
			"current_state": REF("GrievanceCurrentState"),
			"timeline_event": OBJ(
				{
					"name": S(nullable=True),
					"entry_type": S(),
					"body": S(nullable=True),
					"is_internal": B(),
					"author_role": S(),
					"author_type": S(),
					"from_status": S(nullable=True),
					"to_status": S(nullable=True),
					"created_on": S(format="date-time", nullable=True),
				},
				nullable=True,
				description="Generated timeline event if immediately approved",
			),
			"assigned_dept": S(nullable=True),
			"assigned_to": S(nullable=True),
			"service_category": S(nullable=True),
			"grievance_type": S(nullable=True),
			"sla_due_date": S(format="date-time", nullable=True),
			"is_anonymous": B(nullable=True),
			"anonymity_status": S(nullable=True),
		},
		required=["ticket_number", "status", "change_request", "current_state"],
		description="Outcome of a change-request-backed modification on a grievance",
	),
)

data(
	"CategoryAssignment",
	OBJ(
		{
			"name": S(
				example="GR-RBAC-00001",
				description="Grievance RBAC Assignment id for this department and category",
			),
			"service_category": S(example="Inputs", description="Grievance service category"),
			"department": S(description="Owning department the category routes to"),
			"l1_officer": S(nullable=True, description="L1 nodal officer user id"),
			"l1_officer_name": S(nullable=True, description="L1 officer display name"),
			"l2_officer": S(nullable=True, description="L2 senior nodal officer user id"),
			"l2_officer_name": S(nullable=True, description="L2 officer display name"),
			"sla_days": I(
				nullable=True,
				minimum=1,
				example=14,
				description="SLA window in days. Shared by every department serving the category.",
			),
			"auto_escalate": B(
				nullable=True,
				description="Escalate automatically when the SLA is breached. Shared by every department serving the category.",
			),
			"active": B(description="False once the rule has been deactivated"),
			"l1_role_level": S(
				nullable=True,
				description="L1 role level copied from the department",
			),
			"l2_role_level": S(
				nullable=True,
				description="L2 role level copied from the department",
			),
			"routing_strategy": S(
				nullable=True,
				enum=["Primary First", "Round Robin", "Least Loaded"],
				description="Routing strategy copied from the department. Null when the department has not chosen one.",
			),
		},
		required=[
			"name",
			"service_category",
			"department",
			"active",
		],
		description="Category-to-department routing rule",
	),
)

data(
	"CategoryAssignmentData",
	OBJ(
		{"assignment": REF("CategoryAssignment")},
		required=["assignment"],
		description="One category assignment",
	),
)

data(
	"CategoryAssignmentListData",
	OBJ(
		{
			"assignments": ARR(REF("CategoryAssignment")),
			"pagination": REF("PaginationMeta"),
		},
		required=["assignments", "pagination"],
		description="One page of category assignments",
	),
)


# Response templates (Administration)
data(
	"AdminResponseTemplate",
	OBJ(
		{
			"template": S(example="TPL-RESOLVED-INPUTS", description="Template code; fixed once created"),
			"title": S(),
			"action": S(
				example="Resolve", nullable=True, description="Grievance workflow action the template is for"
			),
			"workflow_action": S(
				example="Resolve", description="Grievance workflow action the template is for"
			),
			"department": S(nullable=True, description="Null for every department"),
			"service_category": S(nullable=True, description="Null for every category"),
			"reason": S(nullable=True, description="Jinja template for the reason, unrendered"),
			"body": S(description="Jinja template for the reason, unrendered"),
			"reason_parts": {**REF("ResponseParts"), "nullable": True},
			"note": S(nullable=True, description="Jinja template for internal note, unrendered"),
			"usage_count": I(description="Actions sent with this template"),
			"is_active": B(),
		},
		required=["template", "title", "workflow_action", "body", "usage_count", "is_active"],
		description="A response template as administrators manage it",
	),
)

data(
	"AdminResponseTemplateData",
	OBJ({"response_template": REF("AdminResponseTemplate")}, required=["response_template"]),
)

data(
	"AdminResponseTemplateListData",
	OBJ(
		{"response_templates": ARR(REF("AdminResponseTemplate")), "pagination": REF("PaginationMeta")},
		required=["response_templates", "pagination"],
	),
)


# Dashboard charts. Rows differ per chart and are documented on each route; each is
# a flat object of counts, codes and labels, never case detail on the public routes.
data(
	"DashboardChartRow",
	OBJ({}, additionalProperties=True, description="One row of a chart; the keys depend on the chart"),
)
data(
	"DashboardChartsData",
	OBJ(
		{},
		additionalProperties={**ARR(REF("DashboardChartRow")), "nullable": True},
		description="Rows per requested chart id; null for a chart that failed (see meta.errors)",
	),
)

# ---------------------------------------------------------------------------
# Request Schemas
# ---------------------------------------------------------------------------
# Derived from the Pydantic model each route names in @validate_request, so the
# contract is the validation the code runs. Filled by build_openapi().
REQ: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Response Envelopes
# ---------------------------------------------------------------------------
ENVELOPES = {
	"HealthResponse": make_envelope("HealthData", description="Health check response"),
	"PingResponse": make_envelope("PingData", description="Ping response"),
	"SubmitterOptionsResponse": make_envelope(
		"SubmitterOptionsData", description="Submitter options response"
	),
	"SubmitterProfileResponse": make_envelope(
		"SubmitterProfileData", description="Submitter profile response"
	),
	"SubmitterRegisterResponse": make_envelope(
		"SubmitterRegisterResultData", description="Submitter registration response"
	),
	"SubmitterBlockResponse": make_envelope(
		"SubmitterBlockResultData", description="Submitter block/unblock response"
	),
	"AdministrativeAreasListResponse": make_envelope(
		"AdministrativeAreasListData", description="Administrative areas list response"
	),
	"AreaAncestorsResponse": make_envelope("AreaAncestorsData", description="Area ancestors response"),
	"DraftResponse": make_envelope("DraftData", nullable_data=True, description="Grievance draft response"),
	"DraftSubmitResultResponse": make_envelope(
		"GrievanceSubmitResultData", description="Grievance draft submission outcome response"
	),
	"DraftDiscardResponse": make_envelope("DraftDiscardData", description="Draft discard outcome response"),
	"GrievanceSubmitResultResponse": make_envelope(
		"GrievanceSubmitResultData", description="Grievance submission outcome response"
	),
	"GrievanceListResponse": make_envelope(
		"GrievanceListData", description="Paginated grievance list response"
	),
	"GrievanceDetailResponse": make_envelope("GrievanceDetailData", description="Grievance details response"),
	"GrievanceOptionsResponse": make_envelope(
		"GrievanceOptionsData", description="Grievance options response"
	),
	"GrievanceStatusSummaryResponse": make_envelope(
		"GrievanceStatusSummaryData", description="KPI status card counts"
	),
	"ResponseTemplatesResponse": make_envelope(
		"ResponseTemplatesData", description="Rendered response templates"
	),
	"GrievanceTimelineResponse": make_envelope(
		"GrievanceTimelineData", description="Grievance timeline response"
	),
	"GrievanceActionResultResponse": make_envelope(
		"GrievanceActionResultData", description="Action result response"
	),
	"GrievanceMessageResponse": make_envelope(
		"GrievanceMessageData", description="Message or internal note posted"
	),
	"AdminResponseTemplateResponse": make_envelope(
		"AdminResponseTemplateData", description="One response template"
	),
	"AdminResponseTemplateListResponse": make_envelope(
		"AdminResponseTemplateListData", description="One page of response templates"
	),
	"ChangeRequestResponse": make_envelope(
		"ChangeRequestData", description="Grievance change request response"
	),
	"ChangeRequestListResponse": make_envelope(
		"ChangeRequestListData", description="List of change requests response"
	),
	"GrievanceChangeResponse": make_envelope(
		"GrievanceChangeResponseData", description="Grievance change action response"
	),
	"AttachmentUploadResponse": make_envelope(
		"AttachmentItem", is_list=True, description="Attachment upload response"
	),
	"AttachmentListResponse": make_envelope(
		"AttachmentItem", is_list=True, description="Attachment list response"
	),
	"AttachmentDownloadResponse": make_envelope(
		"AttachmentDownloadData", description="Attachment download URL response"
	),
	"DeleteAttachmentResponse": make_envelope(
		"DeleteAttachmentData", description="Attachment deletion response"
	),
	"CategoryAssignmentResponse": make_envelope(
		"CategoryAssignmentData", description="Category assignment response"
	),
	"CategoryAssignmentListResponse": make_envelope(
		"CategoryAssignmentListData",
		description="Category assignment list response",
	),
	"DashboardChartResponse": make_envelope(
		"DashboardChartRow",
		is_list=True,
		description="Rows of one public dashboard chart; meta.as_of is the rollup time",
	),
	"DashboardChartsResponse": make_envelope(
		"DashboardChartsData", description="Rows per chart; meta.as_of and meta.errors per chart"
	),
}


# ---------------------------------------------------------------------------
# Query Parameters Catalog
# ---------------------------------------------------------------------------
QP: dict[str, list[dict[str, Any]]] = {
	"SubmitterOptions": [
		{
			"name": "search_country",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Filter phone ISD prefixes",
		},
		{
			"name": "country",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Exact country name or 2-letter ISO code",
		},
		{
			"name": "include_phone_extensions",
			"in": "query",
			"required": False,
			"schema": B(default=True),
			"description": "Include phone dialing codes",
		},
		{
			"name": "service_category",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Filter grievance types by category",
		},
	],
	"GrievanceOptions": [
		{
			"name": "service_category",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Filter grievance types and officers by service category",
		},
		{
			"name": "category",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Alias for service_category",
		},
		{
			"name": "department",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Department ID to cascade and return active assigned officers",
		},
		{
			"name": "administrative_area",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Administrative area ID to filter officer assignments",
		},
	],
	"AdministrativeAreas": [
		{
			"name": "parent",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Parent area ID, path_code, or comma-separated list of parents (cascading drill-down)",
		},
		{
			"name": "level_name",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Filter by administrative tier (e.g. Region, Zone, Woreda, Kebele)",
		},
		{
			"name": "search",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Free text search by area name or code",
		},
		{
			"name": "ancestors_of",
			"in": "query",
			"required": False,
			"schema": S(),
			"description": "Return breadcrumb chain for specified node",
		},
		{
			"name": "limit",
			"in": "query",
			"required": False,
			"schema": I(default=100, maximum=500),
			"description": "Maximum records returned",
		},
	],
	"ViewAttachment": [
		{
			"name": "download",
			"in": "query",
			"required": False,
			"schema": B(default=False),
			"description": "Send Content-Disposition: attachment (Save As) instead of inline",
		}
	],
}

QP["DashboardChart"] = [
	{
		"name": "region",
		"in": "query",
		"required": False,
		"schema": S(),
		"description": "Comma-separated Region P-codes (e.g. ET04)",
	},
	{
		"name": "service_category",
		"in": "query",
		"required": False,
		"schema": S(),
		"description": "Comma-separated service category names; `category` is accepted as an alias",
	},
	{
		"name": "from",
		"in": "query",
		"required": False,
		"schema": S(format="date"),
		"description": "Period start (trend and category-resolution charts)",
	},
	{
		"name": "to",
		"in": "query",
		"required": False,
		"schema": S(format="date"),
		"description": "Period end, default today",
	},
	{
		"name": "month",
		"in": "query",
		"required": False,
		"schema": S(pattern=r"^\d{4}-\d{2}$"),
		"description": "YYYY-MM for grvPerformanceKpis, default the current month",
	},
	{
		"name": "granularity",
		"in": "query",
		"required": False,
		"schema": S(enum=["month", "week"], default="month"),
		"description": "Period size for grvNetBacklogTrend",
	},
]

# The admin form takes several charts at once and two filters the public one does not.
QP["DashboardCharts"] = [
	{
		"name": "charts",
		"in": "query",
		"required": False,
		"schema": S(),
		"description": "Comma-separated chart ids (at most 20); all charts when omitted",
	},
	*QP["DashboardChart"],
	{
		"name": "assigned_dept",
		"in": "query",
		"required": False,
		"schema": S(),
		"description": "Comma-separated department names",
	},
	{
		"name": "limit",
		"in": "query",
		"required": False,
		"schema": I(minimum=1, maximum=50, default=10),
		"description": "Rows for grvRecent",
	},
]

# The OAN dashboards read the public charts as a Frappe user holding Grievance
# Dashboard Reader, with that user's API key and secret (FrappeTokenAuth below).
PUBLIC_CHART_SECURITY = [{"FrappeTokenAuth": []}]


# ---------------------------------------------------------------------------
# Dynamic Discovery & Spec Builder
# ---------------------------------------------------------------------------
# Requests that carry files. The others are JSON only.
MULTIPART_REQUESTS = {
	"SubmitDocumentsRequest": ("multipart/form-data",),
	"GrievanceActionRequest": ("application/json", "multipart/form-data"),
	"PostMessageRequest": ("application/json", "multipart/form-data"),
}


def _import_all_api_modules() -> None:
	"""Import all endpoint modules so Werkzeug rules are fully populated."""
	api_modules = [
		"oan_grievance_service.api.router",
		"oan_grievance_service.api.v1.administrative_area",
		"oan_grievance_service.api.v1.attachment",
		"oan_grievance_service.api.v1.category_assignment",
		"oan_grievance_service.api.v1.change_request",
		"oan_grievance_service.api.v1.charts",
		"oan_grievance_service.api.v1.draft",
		"oan_grievance_service.api.v1.grievance",
		"oan_grievance_service.api.v1.officer",
		"oan_grievance_service.api.v1.profile",
		"oan_grievance_service.api.v1.response_config",
		"oan_grievance_service.api.v1.submitter",
	]
	for mod_name in api_modules:
		try:
			importlib.import_module(mod_name)
		except Exception as e:
			print(f"Warning: Failed to import {mod_name}: {e}", file=sys.stderr)


def _determine_tag(path: str, func_name: str) -> str:
	if "/health" in path or "/ping" in path:
		return "Health & Monitoring"
	if path.startswith("/api/v1/submitters"):
		return "Submitter Management"
	if path.startswith("/api/v1/administrative-areas"):
		return "Administrative Areas"
	if path.startswith("/api/v1/drafts"):
		return "Grievance Drafts"
	if path.startswith(("/api/v1/category-assignments", "/api/v1/officers", "/api/v1/response-templates")):
		return "Administration"
	if path.startswith("/api/v1/charts"):
		return "Dashboard Charts"
	if "/change-requests" in path or path.startswith("/api/v1/change-requests"):
		return "Change Requests"
	if "/attachments" in path or path.startswith("/api/v1/attachments"):
		return "Attachments"
	if any(
		kw in path
		for kw in (
			"action",
			"message",
			"timeline",
			"reassign",
			"defer-sla",
			"anonymity-decision",
			"response-templates",
		)
	):
		return "Grievance Lifecycle & Actions"
	return "Grievances Core"


def _determine_response(func_name: str, path: str, method: str) -> str | None:
	mapping = {
		"get_health": "HealthResponse",
		"get_ping": "PingResponse",
		"get_areas": "AdministrativeAreasListResponse",
		"get_area_ancestors": "AreaAncestorsResponse",
		"list_grievances": "GrievanceListResponse",
		"submit": "GrievanceSubmitResultResponse",
		"action": "GrievanceActionResultResponse",
		"timeline": "GrievanceTimelineResponse",
		"response_templates": "ResponseTemplatesResponse",
		"message": "GrievanceMessageResponse",
		"reassign": "GrievanceChangeResponse",
		"defer_sla": "GrievanceChangeResponse",
		"decide_deferral": "ChangeRequestResponse",
		"decide_reassignment": "ChangeRequestResponse",
		"anonymity_decision": "GrievanceChangeResponse",
		"summary": "GrievanceStatusSummaryResponse",
		"options": "SubmitterOptionsResponse" if "submitters" in path else "GrievanceOptionsResponse",
		"me": "SubmitterProfileResponse",
		"submit_documents": "AttachmentUploadResponse",
		"get_attachments": "AttachmentListResponse",
		"download": "AttachmentDownloadResponse",
		"view": None,  # Binary stream
		"delete": "DeleteAttachmentResponse",
		"list_requests": "ChangeRequestListResponse",
		"get_request": "ChangeRequestResponse",
		"decide": "ChangeRequestResponse",
		"save": "DraftResponse",
		"load": "DraftResponse",
		"submit_draft": "DraftSubmitResultResponse",
		"discard": "DraftDiscardResponse",
		"register_submitter": "SubmitterRegisterResponse",
		"block_submitter": "SubmitterBlockResponse",
		"unblock_submitter": "SubmitterBlockResponse",
		"get_charts": "DashboardChartsResponse",
		"list_assignments": "CategoryAssignmentListResponse",
		"get_assignment": "CategoryAssignmentResponse",
		"create_assignment": "CategoryAssignmentResponse",
		"update_assignment": "CategoryAssignmentResponse",
		"deactivate_assignment": "CategoryAssignmentResponse",
		"list_response_templates": "AdminResponseTemplateListResponse",
		"get_response_template": "AdminResponseTemplateResponse",
		"create_response_template": "AdminResponseTemplateResponse",
		"update_response_template": "AdminResponseTemplateResponse",
		"deactivate_response_template": "AdminResponseTemplateResponse",
	}
	if func_name.startswith("get_public_chart_"):
		return "DashboardChartResponse"
	return mapping.get(func_name, "GrievanceActionResultResponse" if method == "POST" else None)


def build_openapi() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
	_import_all_api_modules()

	paths: dict[str, Any] = {}
	seen_ops: set[tuple[str, str]] = set()

	for rule in _rules:
		# Convert Werkzeug pattern e.g. <path:area_id_or_path> to {area_id_or_path}
		openapi_path = re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", rule.rule)
		path_param_names = re.findall(r"<(?:\w+:)?(\w+)>", rule.rule)

		methods = [m.upper() for m in rule.methods if m.upper() != "HEAD"]
		endpoint_fn = rule.endpoint
		unwrapped = inspect.unwrap(endpoint_fn)
		func_name = unwrapped.__name__
		legacy_target = f"{unwrapped.__module__}.{func_name}"

		route_info = getattr(endpoint_fn, "_route", {})
		allow_guest = route_info.get("allow_guest", False) or rule.rule in _exempt_paths
		summary = route_info.get("summary") or (unwrapped.__doc__ or "").strip().split("\n")[0]
		description = (unwrapped.__doc__ or "").strip() or summary

		tag = _determine_tag(openapi_path, func_name)
		req_model = request_model(endpoint_fn)
		req_schema_name = req_model.__name__ if req_model else None
		req_schema = request_schema(req_model, path_param_names, REQ) if req_model else None

		for method in sorted(methods):
			op_key = (method, openapi_path)
			if op_key in seen_ops:
				continue
			seen_ops.add(op_key)

			if openapi_path not in paths:
				paths[openapi_path] = {}

			# Path parameters
			parameters: list[dict[str, Any]] = []
			for p in path_param_names:
				param_desc = f"{p.replace('_', ' ').title()} identifier"
				if p == "ticket_number":
					param_desc = "Unique grievance ticket number (e.g. 3-001-002A-0 or 3001002A0)"
				elif p == "attachment":
					param_desc = "Unique attachment record ID"
				elif p == "area_id_or_path":
					param_desc = (
						"Area ID (e.g. 'kebele-ET140108101008') or path_code ('ET.ET14.01.08.101.008')"
					)

				parameters.append(
					{
						"name": p,
						"in": "path",
						"required": True,
						"schema": S(),
						"description": param_desc,
					}
				)

			# Query parameters: from the request model, else the catalog for the few
			# GET routes that read their query string without one.
			if method == "GET":
				if req_schema:
					parameters.extend(query_parameters(req_schema))
				elif func_name == "get_areas":
					parameters.extend(copy.deepcopy(QP["AdministrativeAreas"]))
				elif func_name == "options" and "submitters" in openapi_path:
					parameters.extend(copy.deepcopy(QP["SubmitterOptions"]))
				elif func_name == "options" and "grievances" in openapi_path:
					parameters.extend(copy.deepcopy(QP["GrievanceOptions"]))
				elif func_name == "view":
					parameters.extend(copy.deepcopy(QP["ViewAttachment"]))
				elif func_name == "get_charts":
					parameters.extend(copy.deepcopy(QP["DashboardCharts"]))
				elif func_name.startswith("get_public_chart_"):
					parameters.extend(copy.deepcopy(QP["DashboardChart"]))

			response_schema_name = _determine_response(func_name, openapi_path, method)
			resp_content_type = "*/*" if func_name == "view" else "application/json"

			op: dict[str, Any] = {
				"tags": [tag],
				"summary": summary,
				"description": description,
				"operationId": f"{method.lower()}_{openapi_path.strip('/').replace('/', '_').replace('-', '_').replace('{', '').replace('}', '')}",
				"responses": {
					"200": {
						"description": "Success",
						"content": {
							resp_content_type: {
								"schema": REF(response_schema_name) if response_schema_name else BINARY
							}
						},
					},
					"400": {
						"description": "Validation or Bad Input Error",
						"content": {"application/json": {"schema": REF("StandardErrorResponse")}},
					},
					"401": {
						"description": "Unauthorized / Authentication Required",
						"content": {"application/json": {"schema": REF("StandardErrorResponse")}},
					},
					"403": {
						"description": "Forbidden / Insufficient Role Scope",
						"content": {"application/json": {"schema": REF("StandardErrorResponse")}},
					},
					"404": {
						"description": "Resource Not Found",
						"content": {"application/json": {"schema": REF("StandardErrorResponse")}},
					},
					"500": {
						"description": "Internal Server Error",
						"content": {"application/json": {"schema": REF("StandardErrorResponse")}},
					},
				},
			}

			if parameters:
				op["parameters"] = parameters

			op["x-legacy-rpc-method"] = legacy_target
			if func_name.startswith("get_public_chart_"):
				op["security"] = copy.deepcopy(PUBLIC_CHART_SECURITY)
			else:
				op["security"] = [] if allow_guest else [{"BearerAuth": []}]

			# Request body for mutation methods
			# Request body. A model left empty once the path parameters are taken out
			# (the *Ref models) means the operation takes no body.
			if method in ("POST", "PUT", "PATCH", "DELETE") and req_schema:
				body_schema = copy.deepcopy(req_schema)
				if req_schema_name in MULTIPART_REQUESTS:
					body_schema["properties"]["files"] = ARR(
						BINARY, description="Files, sent as multipart/form-data"
					)
				component = req_schema_name
				if REQ.get(component, body_schema) != body_schema:
					# The same model on routes with different path parameters.
					component = f"{req_schema_name}_{'_'.join(path_param_names) or 'body'}"
				REQ[component] = body_schema
				content_types = MULTIPART_REQUESTS.get(req_schema_name, ("application/json",))
				op["requestBody"] = {
					"required": bool(body_schema.get("required")),
					"content": {ct: {"schema": REF(component)} for ct in content_types},
				}

			paths[openapi_path][method.lower()] = op

	components_schemas: dict[str, Any] = {}
	components_schemas.update(DATA_SCHEMAS)
	components_schemas.update(REQ)
	components_schemas.update(ENVELOPES)

	doc: dict[str, Any] = {
		"openapi": "3.0.3",
		"info": {
			"title": "OAN Grievance Service API",
			"version": "1.0.0",
			"description": (
				"Grievance management and citizen feedback service for OpenAgriNet (OAN). "
				+ "Provides RESTful endpoints for submitting complaints, tracking resolution progress, "
				+ "cascading administrative area drill-downs, citizen-officer timeline messaging, "
				+ "escalation management, change requests, and case resolution workflows."
			),
			"contact": {"name": "COSS - Centre for Open Societal Systems"},
		},
		"servers": [
			{"url": "http://localhost:8000", "description": "Local Frappe Bench"},
			{"url": "https://grievance.openagrinet.org", "description": "Production Grievance Gateway"},
		],
		"tags": [
			{"name": "Health & Monitoring", "description": "Service health probes and uptime pings"},
			{
				"name": "Submitter Management",
				"description": "Intake reference options, submitter registration, and moderation",
			},
			{
				"name": "Administrative Areas",
				"description": "Cascading geographic drill-downs, breadcrumbs, and search",
			},
			{
				"name": "Grievance Drafts",
				"description": "Draft grievance persistence, resume, submit, and discard",
			},
			{"name": "Grievances Core", "description": "Case intake, tracking, and filtered list views"},
			{
				"name": "Change Requests",
				"description": "Request, review, and decide on grievance field changes (department, officer, SLA deferral, anonymity)",
			},
			{
				"name": "Grievance Lifecycle & Actions",
				"description": "Communication threads, notes, reopen, reject, escalate, and resolution confirmation",
			},
			{
				"name": "Attachments",
				"description": "Supporting document and evidence upload, listing, download, and deletion",
			},
			{
				"name": "Administration",
				"description": "Category-to-department routing rules, and the response templates officers respond with",
			},
			{
				"name": "Dashboard Charts",
				"description": "Aggregate counts from the 15-minute rollups, for the OAN programme dashboards",
			},
		],
		"paths": paths,
		"components": {
			"securitySchemes": {
				"BearerAuth": {
					"type": "http",
					"scheme": "bearer",
					"bearerFormat": "JWT",
					"description": "Provide JWT access token as `Bearer <token>` in the Authorization header.",
				},
				"FrappeTokenAuth": {
					"type": "apiKey",
					"in": "header",
					"name": "Authorization",
					"description": "Frappe API key and secret of a user holding Grievance Dashboard Reader, sent as `token <api_key>:<api_secret>`. Generated on that user in the desk and checked by the platform; the gateway passes the header through.",
				},
			},
			"schemas": components_schemas,
		},
	}
	return doc, paths, components_schemas


def main() -> None:
	doc, paths, components_schemas = build_openapi()
	n_paths = len(paths)
	n_ops = sum(len(v) for v in paths.values())

	dump_spec(
		doc,
		INTERNAL_SPEC_OUTPUT,
		[
			"OAN Grievance Service API -- OpenAPI 3.0.3 (INTERNAL)",
			"Carries internal vendor extensions (x-legacy-rpc-method).",
			"Dynamically generated from generate_openapi_spec.py -- do not edit manually.",
		],
	)
	print(
		f"Wrote {INTERNAL_SPEC_OUTPUT.name}: {n_paths} paths, {n_ops} operations, {len(components_schemas)} schemas",
		file=sys.stderr,
	)

	dump_spec(
		strip_extensions(doc),
		PUBLIC_SPEC_OUTPUT,
		[
			"OAN Grievance Service API -- OpenAPI 3.0.3 (PUBLIC)",
			"Contract with vendor extensions removed. Dynamically generated from generate_openapi_spec.py.",
		],
	)
	print(
		f"Wrote {PUBLIC_SPEC_OUTPUT.name}: {n_paths} paths, {n_ops} operations, {len(components_schemas)} schemas",
		file=sys.stderr,
	)


if __name__ == "__main__":
	main()
