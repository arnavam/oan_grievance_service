# API Development Standards & Best Practices

This document defines the architectural conventions, decorator pipeline, request validation rules, error handling, versioning policy, and test requirements for writing new REST API endpoints in `oan_grievance_service`.

---

## 1. Core Architecture & Design Principles

1. **Thin API Layer, Rich Service Layer:** API handlers must act solely as HTTP gateways. They perform authentication, request validation, invoke domain service methods (in `oan_grievance_service/services/`), and format the response envelope. **Do not write direct SQL or business logic inside API handlers.**
2. **Immutable Versioning:** All public endpoints live under a versioned package (`oan_grievance_service/api/v1/`).
   - Additive, backward-compatible fields remain in `v1`.
   - Breaking changes (field renaming, type changes, narrowing constraints) require opening a new version package (e.g. `v2/`).
   - Never modify or break an existing released version.
3. **Consistent Response Envelopes:** Every endpoint returns standard responses via `success_response()`, with version metadata (`meta`) automatically attached by `@handle_api_errors`.
4. **Auditable & Non-Bypassing:** All operations that mutate DocType state must route through standard Frappe document methods or service hooks so workflow guards, timeline logs, and status history are preserved.

---

## 2. Directory Structure & URL Mapping

Endpoints follow Frappe's method dispatch convention:

```
oan_grievance_service/api/
├── __init__.py                # Version registry & metadata helpers
├── middleware.py              # JWT validation & public path exemptions
└── v1/                        # Version 1 API package
    ├── __init__.py            # Module index & endpoint catalog
    ├── grievance.py           # Case submission, tracking & lifecycle actions
    ├── submitter.py           # Submitter profile & lookup options
    └── administrative_area.py # Cascading geo-hierarchy lookups
```

**Dispatch Path:**  
A function `submit` in `oan_grievance_service/api/v1/grievance.py` maps to:  
`POST /api/method/oan_grievance_service.api.v1.grievance.submit`

---

## 3. Decorator Pipeline & Execution Order

Every API endpoint must apply decorators in the exact order shown below:

```python
@frappe.whitelist()                               # 1. Exposes method via HTTP RPC
@handle_api_errors                                # 2. Catches exceptions and formats error JSON
@require_role(ALLOWED_ROLES)                      # 3. Enforces RBAC permissions
@validate_request(YourRequestModel)               # 4. Validates payload schema via Pydantic
def your_endpoint(**kwargs):
    ...
```

### Decorator Responsibilities

| Decorator                  | Source                       | Purpose                                                                                                                                            |
| -------------------------- | ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `@frappe.whitelist()`      | `frappe`                     | Whitelists the Python function for HTTP invocation. Use `allow_guest=True` only for public, unauthenticated routes.                                |
| `@handle_api_errors`       | `oan_auth_service.api.utils` | Intercepts `frappe.ValidationError`, `frappe.PermissionError`, etc., and returns standard JSON error responses with appropriate HTTP status codes. |
| `@require_role(...)`       | `oan_auth_service.api.utils` | Blocks requests if the authenticated user lacks one of the specified roles (e.g., `Grievance Submitter`, `Grievance Officer`).                     |
| `@validate_request(Model)` | `oan_auth_service.api.utils` | Validates input against a Pydantic schema before executing the handler.                                                                            |

---

## 4. Request Validation with Pydantic

All `POST` / mutation endpoints must define an explicit `pydantic.BaseModel` schema.

```python
from pydantic import BaseModel, Field
from oan_auth_service.api.utils import RequiredPhone, SafeEmail

class SubmitGrievanceRequest(BaseModel):
	model_config = {"extra": "allow"}

	submitter_type: str = Field(..., min_length=1, description="Type of submitter")
	submitter_name: str = Field(..., min_length=1, description="Full name of citizen/org")
	contact_mobile: RequiredPhone
	submission_channel: str = Field(..., min_length=1)
	administrative_area: str = Field(..., min_length=1)
	service_category: str = Field(..., min_length=1)
	grievance_type: str = Field(..., min_length=1)
	description: str = Field(..., min_length=20)
	contact_email: SafeEmail | None = None
	assisted_by_officer: str | None = None
	is_anonymous: int | None = Field(0, ge=0, le=1)
```

### Guidelines for Schemas

- Use `RequiredPhone` and `SafeEmail` utility types from `oan_auth_service.api.utils`.
- Enforce sensible length and boundary constraints using `Field(..., min_length=...)` or `ge`/`le`.
- Set `model_config = {"extra": "allow"}` if forward compatibility with client parameters is required, or `"forbid"` if strict parameter policing is desired.

---

## 5. Response Format & Standard Envelopes

All successful responses **MUST** use the `success_response()` helper from `oan_auth_service.api.utils`. `@handle_api_errors` automatically resolves and attaches the `meta` block directly from `api/__init__.py`.

```python
from oan_auth_service.api.utils import handle_api_errors, success_response

@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
@handle_api_errors
def options():
	# ...
	return success_response(
		data=data,
		message=_("Options fetched successfully"),
	)
```

### Standard Success Response Payload

```json
{
  "status": "success",
  "message": "Options fetched successfully",
  "data": {
    "submitter_types": [ ... ],
    "submission_types": [ ... ]
  },
  "meta": {
    "api_version": "v1",
    "status": "current"
  },
  "request_id": "8fa1e19d-b4ef-4bbd-9866-9dc7bc5fec1b"
}
```

---

## 6. Authentication & Public Route Exemption

1. **Authenticated by Default:** Requests hitting `/api/method/oan_grievance_service.*` are validated via JWT tokens handled by `oan_auth_service`.
2. **Public Routes (Unauthenticated):** If an endpoint must be accessible without a login or token (e.g. dropdown lookups, public search):
   - Add `@frappe.whitelist(allow_guest=True)` with the `# nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method` annotation.
   - Ensure all parameters on whitelisted functions have **explicit type hints** (e.g., `parent: str | None = None, limit: int = 100`).
   - **Explicitly register the endpoint path** in `oan_grievance_service/api/middleware.py`:

```python
EXEMPT_PATHS: list[str] = [
	"/api/method/oan_grievance_service.api.v1.submitter.options",
	"/api/method/oan_grievance_service.api.v1.administrative_area.get_areas",
	"/api/method/oan_grievance_service.api.v1.your_module.public_endpoint",
]
```

---

## 7. Error Handling & Validation Failures

Always raise standard Frappe exceptions with clear, localized messages and titles. `@handle_api_errors` handles the conversion to JSON.

```python
# Validation / Bad Input -> Returns HTTP 400
if not frappe.db.exists("Service Category", kwargs["service_category"]):
	frappe.throw(
		_("The specified service category does not exist."),
		exc=frappe.ValidationError,
		title=_("Invalid Category"),
	)

# Record Not Found -> Returns HTTP 404
if not frappe.db.exists("Grievance", {"ticket_number": ticket_number}):
	frappe.throw(
		_("Ticket #{0} was not found.").format(ticket_number),
		exc=frappe.DoesNotExistError,
		title=_("Grievance Not Found"),
	)

# Permission / Authorization Error -> Returns HTTP 403
if not user_has_scope_access(user, grievance):
	frappe.throw(
		_("You do not have permission to access this grievance."),
		exc=frappe.PermissionError,
		title=_("Forbidden"),
	)
```

---

## 8. Complete Boilerplate Template for a New API

Here is a full, production-ready template to use when creating a new API file:

```python
"""<Module description and FSD reference>."""

import frappe
from frappe import _
from pydantic import BaseModel, Field
from oan_auth_service.api.utils import handle_api_errors, require_role, success_response, validate_request

from oan_grievance_service.services import your_service_module

ALLOWED_ROLES = [
	"Grievance Submitter",
	"Grievance Officer",
	"Grievance Admin",
	"System Manager",
	"Administrator",
]


class ExampleActionRequest(BaseModel):
	model_config = {"extra": "allow"}

	ticket_number: str = Field(..., min_length=1, description="Ticket number of the grievance")
	reason: str = Field(..., min_length=5, description="Reason for the action")


@frappe.whitelist()
@handle_api_errors
@require_role(ALLOWED_ROLES)
@validate_request(ExampleActionRequest)
def perform_action(**kwargs):
	"""Execute the domain action and return the standard response."""
	ticket_number = kwargs["ticket_number"]
	reason = kwargs["reason"]

	# 1. Validation & Record Lookup
	doc = frappe.db.get_value(
		"Grievance",
		{"ticket_number": ticket_number},
		["name", "status", "assigned_officer"],
		as_dict=True,
	)
	if not doc:
		frappe.throw(
			_("Ticket #{0} not found.").format(ticket_number),
			exc=frappe.DoesNotExistError,
			title=_("Not Found"),
		)

	# 2. Invoke Service Layer
	result = your_service_module.process_action(doc.name, reason=reason)

	# 3. Return Standard Response
	return success_response(
		data={
			"ticket_number": ticket_number,
			"status": result.status,
			"updated_at": frappe.utils.now_datetime(),
		},
		message=_("Action performed successfully"),
	)
```

---

## 9. Automated Testing for APIs

Every new API endpoint must have automated tests validating:

1. **Happy Path:** Correct parameters return status 200 with matching `meta` and `data` structures.
2. **Invalid Input:** Missing mandatory fields or malformed data trigger validation errors.
3. **Role Guards:** Requests from unauthorized users fail with permission errors.
4. **Public vs. Protected Checks:** Guest access is permitted on exempt paths and blocked on protected paths.

### Example API Test Case

```python
import frappe
from frappe.tests.utils import FrappeTestCase

class TestAPIEndpoints(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_endpoint_returns_valid_envelope(self):
		from oan_grievance_service.api.v1.submitter import options

		res = options()
		self.assertIn("meta", res)
		self.assertEqual(res["meta"]["api_version"], "v1")
		self.assertIn("data", res)
		self.assertIn("submitter_types", res["data"])
```

---

## 10. Postman Collection Synchronization

When introducing a new API or modifying parameters:

1. Open [`postman/oan_grievance_collection.json`](file:///Users/arnav/Code/frappe_local/frappe-bench/apps/oan_grievance_service/postman/oan_grievance_collection.json).
2. Add the request definition under the appropriate folder with:
   - Method (`POST` / `GET`).
   - URL: `{{base_url}}/api/method/oan_grievance_service.api.v1.<module>.<endpoint>`.
   - Headers: `Authorization: Bearer {{auth_token}}`, `Content-Type: application/json`.
   - Sample request payload and example response body.
