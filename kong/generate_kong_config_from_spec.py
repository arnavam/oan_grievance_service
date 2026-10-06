#!/usr/bin/env python3
"""
generate_kong_config_from_spec.py

Generates the declarative Kong gateway configuration (kong.yml) for the
OAN Grievance Service directly from openapi_v1.public.yaml.

Usage:
    python3 generate_kong_config_from_spec.py > kong.yml
    deck validate -s kong.yml
    deck sync -s kong.yml
"""

import re
import sys
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
SPEC_PATH = REPO_ROOT / "openapi" / "openapi_v1.public.yaml"
OUTPUT_PATH = SCRIPT_DIR / "kong.yml"
GRIEVANCE_UPSTREAM_URL = "http://oan-grievance.internal.svc:8000"
# Frappe's Node socket.io server (`bench socketio`, the `websocket` service in
# frappe_docker). Its events are described in openapi/asyncapi_v1.yaml, not the
# OpenAPI spec, so its route is declared here rather than derived.
GRIEVANCE_SOCKET_UPSTREAM_URL = "http://oan-grievance-websocket.internal.svc:9000"
# The Frappe site the socket server serves. Clients connect to the `/<site>`
# socket.io namespace and the server checks it against this header.
FRAPPE_SITE_NAME = '${{ env "DECK_FRAPPE_SITE_NAME" }}'
SELECT_TAGS = ["oan", "grievance"]

# ---------------------------------------------------------------------------
# Throttling tiers
# ---------------------------------------------------------------------------
TIERS = {
	"public-reference": {
		"limit_by": "ip",
		"minute": 120,
		"hour": 3000,
		"policy": "redis",
		"note": "Public unauthenticated reference data: areas, dropdown options, health probes.",
	},
	"citizen-intake": {
		"limit_by": "consumer",
		"minute": 60,
		"hour": 1000,
		"policy": "redis",
		"note": "Citizen self-service operations: grievance lodging, tracking, replies, messaging, reopen.",
	},
	"public-dashboards": {
		"limit_by": "ip",
		"minute": 120,
		"hour": 3000,
		"policy": "redis",
		"note": "Dashboard charts: counts from the 15-minute rollups, read by the OAN dashboards with their Frappe API key; Kong has no consumer for them, so it limits by address. Same as RATE_LIMIT in api/v1/charts.py.",
	},
	"officer-core": {
		"limit_by": "consumer",
		"minute": 300,
		"hour": 10000,
		"policy": "redis",
		"note": "Back-office and staff triage operations: bulk listing, notes, assignment, rejection, and officer options.",
	},
}

# ---------------------------------------------------------------------------
# Explicit Tier assignments per route
# ---------------------------------------------------------------------------
TIER_OVERRIDES = {
	("GET", "/api/v1/grievances/health"): "public-reference",
	("GET", "/api/v1/grievances/ping"): "public-reference",
	("GET", "/api/v1/submitters/options"): "public-reference",
	("POST", "/api/v1/submitters"): "citizen-intake",
	("POST", "/api/v1/submitters/register"): "citizen-intake",
	("POST", "/api/v1/submitters/{profile_id}/block"): "officer-core",
	("POST", "/api/v1/submitters/{profile_id}/unblock"): "officer-core",
	("GET", "/api/v1/administrative-areas"): "public-reference",
	("GET", "/api/v1/administrative-areas/{area_id_or_path}/ancestors"): "public-reference",
	("POST", "/api/v1/drafts"): "citizen-intake",
	("GET", "/api/v1/drafts"): "citizen-intake",
	("POST", "/api/v1/drafts/submit"): "citizen-intake",
	("DELETE", "/api/v1/drafts"): "citizen-intake",
	("GET", "/api/v1/grievances"): "officer-core",
	("POST", "/api/v1/grievances"): "citizen-intake",
	("GET", "/api/v1/grievances/summary"): "officer-core",
	("GET", "/api/v1/grievances/options"): "officer-core",
	("POST", "/api/v1/grievances/{ticket_number}/action"): "citizen-intake",
	("POST", "/api/v1/grievances/{ticket_number}/feedback"): "citizen-intake",
	("POST", "/api/v1/grievances/{ticket_number}/message"): "citizen-intake",
	("POST", "/api/v1/grievances/{ticket_number}/reassign"): "officer-core",
	("POST", "/api/v1/grievances/{ticket_number}/reassign/decide"): "officer-core",
	("POST", "/api/v1/grievances/{ticket_number}/defer-sla"): "officer-core",
	("POST", "/api/v1/grievances/{ticket_number}/defer-sla/decide"): "officer-core",
	("GET", "/api/v1/grievances/{ticket_number}/timeline"): "citizen-intake",
	("GET", "/api/v1/grievances/{ticket_number}/response-templates"): "officer-core",
	("POST", "/api/v1/grievances/{ticket_number}/attachments"): "citizen-intake",
	("GET", "/api/v1/grievances/{ticket_number}/attachments"): "citizen-intake",
	("GET", "/api/v1/attachments"): "officer-core",
	("POST", "/api/v1/attachments"): "citizen-intake",
	("POST", "/api/v1/attachments/{attachment_id}"): "citizen-intake",
	("GET", "/api/v1/attachments/{attachment_id}/download"): "citizen-intake",
	("GET", "/api/v1/attachments/{attachment_id}/view"): "citizen-intake",
	("DELETE", "/api/v1/attachments/{attachment_id}"): "citizen-intake",
	("GET", "/api/v1/change-requests"): "officer-core",
	("GET", "/api/v1/change-requests/{name}"): "officer-core",
	("POST", "/api/v1/change-requests/{name}/decide"): "officer-core",
	("GET", "/api/v1/category-assignments"): "officer-core",
	("POST", "/api/v1/category-assignments"): "officer-core",
	("GET", "/api/v1/category-assignments/{assignment}"): "officer-core",
	("PATCH", "/api/v1/category-assignments/{assignment}"): "officer-core",
	("DELETE", "/api/v1/category-assignments/{assignment}"): "officer-core",
	("GET", "/api/v1/officers"): "officer-core",
	("POST", "/api/v1/officers"): "officer-core",
	("GET", "/api/v1/officers/{officer}"): "officer-core",
	("PATCH", "/api/v1/officers/{officer}"): "officer-core",
	("GET", "/api/v1/response-templates"): "officer-core",
	("POST", "/api/v1/response-templates"): "officer-core",
	("GET", "/api/v1/response-templates/{template}"): "officer-core",
	("PATCH", "/api/v1/response-templates/{template}"): "officer-core",
	("DELETE", "/api/v1/response-templates/{template}"): "officer-core",
	("GET", "/api/v1/charts"): "officer-core",
	**{
		("GET", f"/api/v1/charts/{chart_id}"): "public-dashboards"
		for chart_id in (
			"grvKpis",
			"grvPerformanceKpis",
			"grvMonthlyTrend",
			"grvWeeklyTrend",
			"grvNetBacklogTrend",
			"grvStatusDistribution",
			"grvByCategory",
			"grvCategoryResolution",
			"grvResolutionRateByRegion",
			"grvSlaRisk",
			"grvPendingDuplicates",
			"grvOldestOpen",
			"grvFilterRegions",
			"grvFilterCategories",
		)
	},
}


# Browser origins allowed to call the API, one regex per environment (Kong matches each
# `origins` entry as a regex), e.g. `https://(portal|backoffice)\.openagrinet\.org`.
# Not a secret, since browsers see it in Access-Control-Allow-Origin, but it differs
# per environment, so it comes from the environment at `deck sync`.
CORS_ORIGINS = '${{ env "DECK_CORS_ORIGINS_REGEX" }}'


def load_spec(path):
	with open(path) as f:  # nosemgrep: frappe-security-file-traversal
		return yaml.safe_load(f)


def spec_routes(spec):
	tag_to_domain = {t["name"]: f"d{i + 1:02d}" for i, t in enumerate(spec["tags"])}
	routes = []
	for path, methods in spec["paths"].items():
		for method, op in methods.items():
			method = method.upper()
			security = op.get("security", spec.get("security", []))
			if not security or security == []:
				auth = "public"
			elif security == [{"FrappeTokenAuth": []}]:
				# Frappe checks the API key and secret itself; Kong passes the header on.
				auth = "frappe-token"
			elif any("BearerAuth" in s for s in security):
				auth = "bearer"
			else:
				auth = "custom"
			tag = (op.get("tags") or [None])[0]
			domain = tag_to_domain.get(tag, "d00")
			routes.append(
				{
					"method": method,
					"path": path,
					"auth": auth,
					"domain": domain,
					"tag": tag,
					"operation_id": op.get("operationId"),
				}
			)
	return routes


def reconcile(routes):
	spec_keys = {(r["method"], r["path"]) for r in routes}
	override_keys = set(TIER_OVERRIDES.keys())

	missing_overrides = spec_keys - override_keys
	stale_overrides = override_keys - spec_keys

	if missing_overrides or stale_overrides:
		msg = ["Spec <-> TIER_OVERRIDES mismatch -- refusing to generate kong.yml.", ""]
		if missing_overrides:
			msg.append(f"In the spec but with no assigned throttling tier ({len(missing_overrides)}):")
			for m, p in sorted(missing_overrides):
				msg.append(f"  {m} {p}")
		if stale_overrides:
			msg.append(f"In TIER_OVERRIDES but no longer in the spec ({len(stale_overrides)}):")
			for m, p in sorted(stale_overrides):
				msg.append(f"  {m} {p}")
		msg.append("")
		msg.append("Assign a tier for each route and re-run.")
		raise SystemExit("\n".join(msg))

	for r in routes:
		r["tier"] = TIER_OVERRIDES[(r["method"], r["path"])]
	return routes


def to_kong_regex(path: str) -> str:
	if "{" not in path:
		return f"~{path}$"
	regex = re.sub(r"\{(\w+)\}", r"(?<\1>[^/]+)", path)
	return f"~{regex}$"


def route_name(method: str, path: str) -> str:
	slug = re.sub(r"[{}]", "", path).strip("/").replace("/", "-")
	return f"{method.lower()}-{slug}"[:120]


def build_config(routes):
	service = {
		"name": "oan-grievance-service-v1",
		"url": GRIEVANCE_UPSTREAM_URL,
		"connect_timeout": 5000,
		"write_timeout": 20000,
		"read_timeout": 20000,
		"retries": 2,
		"tags": ["oan", "grievance", "v1"],
		"plugins": [
			{
				"name": "cors",
				"config": {
					"origins": [CORS_ORIGINS],
					"methods": ["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
					"headers": ["Authorization", "Content-Type", "X-Request-Id"],
					"credentials": False,
					"max_age": 3600,
				},
			},
			{
				"name": "request-size-limiting",
				"config": {"allowed_payload_size": 15},  # MB
			},
			{
				"name": "correlation-id",
				"config": {"header_name": "X-Request-Id", "generator": "uuid", "echo_downstream": True},
			},
			{
				"name": "prometheus",
				"config": {"status_code_metrics": True, "latency_metrics": True, "bandwidth_metrics": True},
			},
		],
		"routes": [],
	}

	for r in routes:
		method, path, auth, domain, tier = r["method"], r["path"], r["auth"], r["domain"], r["tier"]
		kong_path = to_kong_regex(path)
		depth = path.count("/")
		route = {
			"name": route_name(method, path),
			"methods": [method],
			"paths": [kong_path],
			"strip_path": False,
			"regex_priority": depth,
			"tags": ["oan", "grievance", "v1", domain, tier, auth],
			"plugins": [],
		}

		t = TIERS[tier]
		route["plugins"].append(
			{
				"name": "rate-limiting",
				"config": {
					"minute": t["minute"],
					"hour": t["hour"],
					"limit_by": t["limit_by"],
					"policy": t["policy"],
					"fault_tolerant": True,
					"hide_client_headers": False,
				},
			}
		)

		if auth == "bearer":
			route["plugins"].append(
				{
					"name": "jwt",
					"config": {
						"claims_to_verify": ["exp"],
						"key_claim_name": "iss",
						"header_names": ["Authorization"],
					},
				}
			)

		service["routes"].append(route)

	# oan-auth-jwt-issuer is owned by oan_auth_service's kong.yml, which also holds
	# its jwt_secrets. Declaring it here too would clash with that repo's slice.
	consumers = [
		{
			"username": "oan-citizen-mobile-client",
			"tags": ["oan", "grievance", "mobile"],
		},
		{
			"username": "oan-backoffice-portal",
			"tags": ["oan", "grievance", "portal"],
		},
	]

	doc = {
		"_format_version": "3.0",
		"_transform": True,
		# decK reads, diffs and deletes only entities carrying every tag listed
		# here, so syncing this file never touches another service's routes or
		# consumers on a shared Kong.
		"_info": {"select_tags": SELECT_TAGS},
		"services": [service, build_socket_service()],
		"consumers": consumers,
	}
	return doc


def build_socket_service():
	"""The realtime socket.io service, see openapi/asyncapi_v1.yaml.

	Frappe's socket server authenticates a socket by replaying its Authorization
	header against frappe.realtime.get_user_info, and refuses any handshake whose
	Origin host differs from its Host. Neither suits a gateway: a browser cannot set
	headers on a WebSocket, and a native app sends no Origin. So Kong takes the JWT
	from the header or the `access_token` query parameter, verifies it, and hands it
	on as a header, and it states the Origin itself. Rewriting Origin is safe only
	because the jwt plugin runs first and the Cookie header is dropped: no request
	reaches Node on ambient browser credentials, which is the attack the Origin
	check exists to stop.
	"""
	return {
		"name": "oan-grievance-realtime-v1",
		"url": GRIEVANCE_SOCKET_UPSTREAM_URL,
		"connect_timeout": 5000,
		# A socket sits idle between events. socket.io pings every 25 seconds, so a
		# live connection never reaches these; a dead one is reaped after an hour.
		"write_timeout": 3600000,
		"read_timeout": 3600000,
		"retries": 0,
		"tags": ["oan", "grievance", "v1", "realtime"],
		"plugins": [
			{
				"name": "correlation-id",
				"config": {"header_name": "X-Request-Id", "generator": "uuid", "echo_downstream": True},
			},
			{
				"name": "prometheus",
				"config": {"status_code_metrics": True, "latency_metrics": True, "bandwidth_metrics": True},
			},
		],
		"routes": [
			{
				"name": "realtime-socket-io",
				"paths": ["/socket.io/"],
				"protocols": ["http", "https"],
				"strip_path": False,
				# The Origin rewrite below copies the public host; Node compares the two.
				"preserve_host": True,
				"tags": ["oan", "grievance", "v1", "realtime", "bearer"],
				"plugins": [
					{
						# Handshakes, not messages: clients use the websocket transport,
						# so each connection is one request. Limited by address because
						# every JWT maps to the one issuer consumer.
						"name": "rate-limiting",
						"config": {
							"minute": 60,
							"hour": 1000,
							"limit_by": "ip",
							"policy": "redis",
							"fault_tolerant": True,
							"hide_client_headers": False,
						},
					},
					{
						"name": "jwt",
						"config": {
							"claims_to_verify": ["exp"],
							"key_claim_name": "iss",
							"header_names": ["Authorization"],
							"uri_param_names": ["access_token"],
						},
					},
					{
						"name": "request-transformer",
						"config": {
							"remove": {"headers": ["Cookie"]},
							"replace": {"headers": ["Origin:https://$(headers.host)"]},
							# `add` only fills a header the client did not send, so a
							# header-borne token is left as it came.
							"add": {
								"headers": [
									"Origin:https://$(headers.host)",
									"Authorization:Bearer $(query_params.access_token)",
									f"X-Frappe-Site-Name:{FRAPPE_SITE_NAME}",
								]
							},
						},
					},
				],
			}
		],
	}


def main():
	spec = load_spec(SPEC_PATH)
	routes = reconcile(spec_routes(spec))
	doc = build_config(routes)

	with open(OUTPUT_PATH, "w") as f:  # nosemgrep: frappe-security-file-traversal
		f.write("# OAN Grievance Service Kong Declarative Config\n")
		f.write(f"# Source Spec: {spec['info']['title']} v{spec['info']['version']}\n")
		f.write("# Generated by generate_kong_config_from_spec.py -- do not hand-edit;\n")
		f.write("# change the OpenAPI spec or TIER_OVERRIDES and re-run.\n")
		yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False, width=100)

	by_method = {}
	for r in routes:
		by_method[r["method"]] = by_method.get(r["method"], 0) + 1
	print(
		f"Wrote {OUTPUT_PATH.name}: {len(routes)} routes from {len(spec['paths'])} paths "
		f"({len(spec['tags'])} domains) -- methods: {by_method}",
		file=sys.stderr,
	)


if __name__ == "__main__":
	main()
