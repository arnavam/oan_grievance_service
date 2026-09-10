"""Versioned public API for the Grievance Management module.

WHY THIS IS VERSIONED
---------------------
FSD 3.2.1 specifies an offline-first Android app that "syncs when connectivity is
restored", and FSD 7 targets farmers on low-connectivity and feature-phone channels.
Both mean app builds stay in the field for months and cannot be force-upgraded. FSD
section 6 additionally commits to bidirectional integration with ATI and MoA systems,
which are third-party release trains this project does not control.

An unversioned endpoint would make every contract change a breaking change for every
deployed client at once. The version lives in the path so an old client keeps its
old contract until it is retired deliberately.

    /api/method/oan_grievance_service.api.v1.grievance.submit

VERSIONING POLICY
-----------------
- A new version is a new package (`v2/`), never an edit to a shipped one.
- Additive changes (a new optional field, a new key in a response) stay in the
  current version. Breaking changes open the next one.
  Breaking means: removing or renaming a field, narrowing an accepted value,
  changing a type, or changing the meaning of an existing field.
- A retired version is announced through `SUNSET`, keeps serving for the notice
  period, then returns HTTP 410. It is never deleted silently.
- Every response carries `meta.api_version`, so a support ticket can name the
  contract the client was actually on.
"""

CURRENT_VERSION = "v1"

# Version registry. `status` is one of: current, deprecated, sunset.
# `sunset_on` is the date a deprecated version starts refusing calls.
VERSIONS = {
	"v1": {
		"status": "current",
		"released_on": "2026-09-04",
		"sunset_on": None,
		"notes": "First public contract. FSD v1.3 D3 FR-02, FR-06, FR-07.",
	},
}


def version_meta(version=CURRENT_VERSION):
	"""The `meta` block every API response carries."""
	info = VERSIONS.get(version, {})
	meta = {"api_version": version, "status": info.get("status")}
	if info.get("sunset_on"):
		meta["sunset_on"] = info["sunset_on"]
	return meta
