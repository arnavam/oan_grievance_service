# Routing, Levels & Department Architecture

How a submitted grievance gets matched to a desk and an officer, how the L1/L2/L3 rungs
cascade, and how escalation picks the next person up. This describes the code as it
actually exists — `services/routing.py`, `services/sla.py::higher_authority_of`, the
`Grievance RBAC Assignment` / `Grievance RBAC Assignment Officer` / `Grievance Role Level`
doctypes — not an aspirational design.

Desk scoping rules, authority lookups (`covering_rows`), the Unrouted fallback desk and
submitter privacy are described in [`desk-scoping-and-authority.md`](desk-scoping-and-authority.md).

## 1. The core records

- **`Grievance RBAC Assignment`** ("desk"): one row per (optional department, optional category,
  optional grievance type, optional service provider, optional administrative area via its
  officer rows). Fields: `department_scope`, `category_scope`, `grievance_type_scope`,
  `service_provider_scope`, `routing_strategy`, `active`, `effective_from`/`effective_to`.
  A desk with no scope on a dimension is unconstrained on it — it matches anything.
- **`Grievance RBAC Assignment Officer`** (child table on the desk): one row per officer
  placed on that desk. Fields: `user`, `role_level` (L1/L2/L3 — a link to
  `Grievance Role Level`, not a hardcoded enum), `reports_to`, `administrative_area`,
  `is_primary`, `active`, `on_leave`, `max_open_cases`, `last_assigned_at`.
- **`Grievance Role Level`**: the rungs themselves, as data rows ordered by `level_order`,
  not an enum. `GrievanceRoleLevel.get_chain()` returns active rungs ascending, cached.
  Inserting a rung between two existing ones is a new row, not a migration.
- **`reports_to`** lives on the officer row, not the desk. Per the officer-management
  contract it is only ever set on an **L1** row, pointing at an **L2** user — it is the
  escalation target for that specific officer, not a department-wide chain.

## 2. Desk matching — which single desk handles a case

`find_matching_assignment(grievance)` (`routing.py:139`):

1. Pull all active desks whose validity window covers today.
2. Keep desks where every _set_ scope field (`category_scope`, `grievance_type_scope`,
   `service_provider_scope`) equals the case's value. An unset scope field always passes.
3. For each surviving desk, find the narrowest officer row whose `administrative_area` is
   an ancestor of (or equal to) the case's area — nested-set containment
   (`area_lft`/`area_rgt`), not a level-by-level walk. A desk whose officers have no area
   set at all matches everything, as the widest possible fit.
4. Rank candidates by `(area_span asc, -specificity)` — narrowest area wins, then most
   constrained scopes. Ties break on oldest desk, then name, so the same case always
   routes identically.

Exactly one desk wins, or none (case stays `Submitted`, manual queue).

## 3. Officer selection — the L1 → L2 → L3 cascade

`pick_officer_by_strategy(assignment_doc, grievance)` (`routing.py:319`), run only against
the _officers on the one desk that won_ step 2 — this is not a walk across desks.

1. Drop Reviewers (read-only, never assigned) and inactive/on-leave officers.
2. Group the rest by rung via `level_order`, ascending.
3. Walk rungs low to high. At each rung, keep only officers whose `administrative_area`
   covers the case (narrowest among that rung — `_covering_case`). Stop at the first rung
   that produces any covering officer.
4. **This is the leaf-without-L1 case**: if the desk's officer rows are all L2 (no L1 row
   exists at all), step 3 finds nothing at rung 1, moves to rung 2, and L2 is treated as
   first-line. No L1 anywhere on that desk is not an error state.
5. Drop anyone who is currently named as another officer's `reports_to` from this rung's
   pool (`supervisors` filter) — a supervisor is reached by escalation, not handed
   first-line work. **In practice this never removes anyone**: `reports_to` is only ever
   set on L1 rows, and step 3 already isolated officers to a single rung, so the
   `reports_to` values present (if any) point to a different rung that isn't in this list.
   If the filter _would_ empty the pool, it's discarded and the whole rung is used instead
   — a second reason it's dead weight.
6. Apply `routing_strategy` within the surviving pool: `Round Robin` (oldest
   `last_assigned_at` first), `Least Loaded` (fewest open, non-concluded cases against
   `max_open_cases`), or `Primary First` (default — `is_primary` wins ties).

## 4. What this cascade is **not**

- Not a walk up a tree of _desks_ (e.g. a woreda desk escalating to a zone desk to a
  region desk). Desks are tried in the order of §4a; the first desk that yields an officer
  wins, and officer selection (§3) never leaves it.
- Not keyed off whether an officer has subordinates. A leaf L2/L3 (nobody reports to them)
  is exactly as eligible as a non-leaf one — see §3 step 5.

### 4a. Desk fallback order

`routing.ranked_assignments` returns desks best first, and `apply_routing` takes the first
one that yields an officer:

1. Desks whose category, type and provider scopes each equal the case's or are empty (an
   empty scope is a wildcard). A desk set to another category, type or provider never takes
   the case.
2. Last, the **Unrouted** desk: the `Unrouted` department, held by the seeded
   `unrouted@grievance.local` officer on the default category (`seed_unrouted_desk`).

Within step 1, the desk whose officers cover the case's area most tightly ranks first,
then desks with more matching scopes (the order `find_matching_assignment` always used).
Region is the last thing given up: a desk with no
officer covering the case's area still qualifies, because the officer pick climbs rungs
until one has no area (usually the department head). If a desk has no officer at all, the
next desk is tried. A case only stays unassigned when the Unrouted desk is deactivated.

## 5. Escalation — what happens after assignment, on SLA breach

Separate mechanism from desk/officer routing, in `sla.py::higher_authority_of`:

1. Prefer the officer's own `reports_to` value, if it points above their current rung.
2. Otherwise find whoever holds the _next_ rung up for the same department/area via
   `find_officer_by_role_level`.
3. `GrievanceRoleLevel.get_chain()` supplies rung order; walking stops at the top of the
   chain (returns `None, None`) or logs an unstaffed rung via `log_unstaffed_for`.

Every "who holds this rung for this case" lookup (`find_officer_by_role_level`, `holds_rung`,
`get_officer_supervisor`, and so `department_head_of` and `is_department_head`) ranks the
candidate desks with `covering_rows`. An empty department, category or area on a desk is a
wildcard, but the most specific desk wins: one naming the case's department and category beats
one naming either, which beats a desk naming neither; then the narrowest covering area. So a
department head with no department or no category is a last resort, never a shortcut past the
department's own head.

This is the only place `reports_to` does real work — a single L1→L2 hop. Everything past
that hop is resolved by rung lookup against the department/area, not by chasing a
`reports_to` chain (there isn't one beyond that first hop; L2/L3 rows don't carry it).

## 6. Async execution

Routing never runs inline on the submit request. `enqueue_routing` enqueues
`route_grievance_job` after commit; the worker re-checks the case is still `Submitted` and
unassigned before calling `apply_routing`. `drain_routing_queue` is the scheduled sweep
that catches anything a failed/missed enqueue left behind.
