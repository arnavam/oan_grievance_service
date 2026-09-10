"""JWT auth_hook configuration for this deployment.

The validation logic itself lives in oan_core. This module exists only to bind
it to oan_grievance_service's own API namespace, exempt paths and revocation rule,
so the shared library never needs to know this app exists.
"""

# Only requests under this prefix are subject to JWT validation; everything else
# (desk, standard Frappe APIs) is left to Frappe's own auth.
API_NAMESPACE = "/api/method/oan_grievance_service."

# Endpoints reachable without a bearer token. Kept explicit rather than pattern
# matched so adding one is a visible diff.
EXEMPT_PATHS: list[str] = [
	"/api/method/oan_grievance_service.api.v1.submitter.options",
	"/api/method/oan_grievance_service.api.v1.administrative_area.get_areas",
]


def validate_jwt_request(request=None):
	"""Entry point registered as `auth_hooks` in hooks.py."""
	raise NotImplementedError
