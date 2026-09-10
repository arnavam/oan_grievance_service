"""Administrative Area cascading lookup and search endpoints for mobile apps, web wizards and public registration."""

import frappe
from oan_auth_service.api.utils import handle_api_errors, success_response


@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
@handle_api_errors
def get_areas(
	parent: str | None = None,
	level_name: str | None = None,
	search: str | None = None,
	ancestors_of: str | None = None,
	limit: int = 100,
):
	"""Public endpoint to fetch administrative areas for cascading dropdowns and searches.

	Query Modes:
	    1. Cascading Drill-Down:
	       - No parent provided: returns all top-level Regions (level_name='Region').
	       - parent provided (e.g. 'region-ET14' or path_code 'ET.ET14'): returns immediate child nodes.
	    2. Level Filter:
	       - level_name provided (e.g. 'Region', 'Zone', 'Woreda', 'Kebele').
	    3. Free-Text Search:
	       - search provided: searches across area_name, code, or path_code.
	    4. Ancestor Breadcrumbs:
	       - ancestors_of provided (area ID or path_code): returns the chain from Country down to the node.

	Args:
	    parent (str, optional): Name or path_code of parent area.
	    level_name (str, optional): Hierarchy tier (Region, Zone, Woreda, Kebele).
	    search (str, optional): Search query matching area name or code.
	    ancestors_of (str, optional): Area name or path_code to fetch ancestor hierarchy for.
	    limit (int, optional): Max records returned (default 100, max 500).

	Returns:
	    List of administrative area dictionaries with ID, area_name, code, path_code, level_name, is_group.
	"""
	limit = min(int(limit or 100), 500)

	# Mode 4: Ancestor chain for breadcrumb rendering
	if ancestors_of:
		return success_response(data=get_ancestors(ancestors_of))

	filters = [["is_active", "=", 1]]

	# Resolve parent ID if a path_code was passed
	if parent:
		if not frappe.db.exists("Administrative Area", parent):
			resolved = frappe.db.get_value("Administrative Area", {"path_code": parent}, "name")
			if resolved:
				parent = resolved
		filters.append(["parent_administrative_area", "=", parent])
	elif not search and not level_name:
		# Default root view: Top-level Regions
		filters.append(["level_name", "=", "Region"])

	if level_name:
		filters.append(["level_name", "=", level_name])

	if search:
		search_term = f"%{search.strip()}%"
		filters.append(["area_name", "like", search_term])

	areas = frappe.get_all(
		"Administrative Area",
		filters=filters,
		fields=[
			"name as area_id",
			"area_name",
			"code",
			"path_code",
			"level_name",
			"parent_administrative_area",
			"is_group",
			"depth",
		],
		order_by="area_name asc",
		limit=limit,
		ignore_permissions=True,
	)

	return success_response(
		data={
			"areas": areas,
			"count": len(areas),
			"parent": parent,
			"level_name": level_name,
		}
	)


def get_ancestors(area_id_or_path):
	"""Fetch ancestor chain from root down to the specified node."""
	node = None
	if frappe.db.exists("Administrative Area", area_id_or_path):
		node = frappe.get_doc("Administrative Area", area_id_or_path)
	else:
		name = frappe.db.get_value("Administrative Area", {"path_code": area_id_or_path}, "name")
		if name:
			node = frappe.get_doc("Administrative Area", name)

	if not node:
		return {"breadcrumbs": [], "current": None}

	ancestors = frappe.get_all(
		"Administrative Area",
		filters=[
			["lft", "<=", node.lft],
			["rgt", ">=", node.rgt],
			["depth", ">", 0],  # skip synthetic World root
		],
		fields=["name as area_id", "area_name", "code", "path_code", "level_name", "depth"],
		order_by="lft asc",
		ignore_permissions=True,
	)

	return {
		"current": {
			"area_id": node.name,
			"area_name": node.area_name,
			"path_code": node.path_code,
			"level_name": node.level_name,
		},
		"breadcrumbs": ancestors,
	}
