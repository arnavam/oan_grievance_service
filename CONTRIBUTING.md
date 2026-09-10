# Developer Setup & PR Standards Guide

This guide outlines the local setup, git hygiene, coding standards, test requirements, and pre-PR verification steps required for contributing to `oan_grievance_service`.

---

## 1. Quick Setup & Prerequisites

### 1.1 Install Developer Dependencies & Git Hooks

Ensure you have developer dependencies installed in your Python / Bench environment:

```bash
# Inside your virtualenv or dev container
pip install pre-commit ruff semgrep
```

### 1.2 Install Pre-Commit and Pre-Push Hooks

Pre-commit hooks automatically format code, sort imports, check syntax, and enforce EOF/whitespace rules on every `git commit`.

```bash
cd apps/oan_grievance_service

# Install standard pre-commit hook (runs on git commit)
pre-commit install

# (Optional but recommended) Install pre-push hook (runs before git push)
pre-commit install --hook-type pre-push
```

### 1.3 Recommended Git Configuration

Set these once on your development machine to simplify merge conflict resolution:

```bash
# Display full 3-way conflict view with base ancestor
git config --global merge.conflictStyle zdiff3

# Enable Git's reuse of recorded conflict resolutions
git config --global rerere.enabled true
```

---

## 2. Branching & Git Hygiene Standards

### 2.1 Branching Strategy

- **Base Branch:** Always branch off the latest `develop` branch.
- **Never commit directly to `develop` or `main`.**

```bash
# 1. Update your local develop branch
git checkout develop
git pull origin develop

# 2. Create your feature/fix branch
git checkout -b <type>/<short-description>
```

### 2.2 Branch Naming Conventions

Use descriptive branch prefixes:

- `feat/<feature-name>`: New feature or DocType additions (e.g., `feat/sla-pause-resume`)
- `fix/<bug-name>`: Bug fixes or guard corrections (e.g., `fix/da-submitter-null-check`)
- `test/<test-scope>`: Adding or improving automated tests (e.g., `test/routing-guards`)
- `docs/<doc-name>`: Documentation updates (e.g., `docs/intake-flow-update`)
- `refactor/<refactor-scope>`: Code refactoring without behavior change

### 2.3 Keeping Your Branch Fresh

Rebase or merge `develop` into your feature branch frequently to prevent merge conflicts:

```bash
git fetch origin develop
git merge origin/develop
```

---

## 3. Coding & Framework Standards

### 3.1 Python Styling & Formatting

We enforce formatting via **Ruff** (configured in `pyproject.toml` and `.pre-commit-config.yaml`):

- **Indentation:** Tabs (`indent-style = "tab"`) — Frappe convention.
- **Quotes:** Double quotes (`quote-style = "double"`).
- **Line Length:** 110 characters.
- **Imports:** Sorted automatically by Ruff (`--select=I`).

To format and lint your code manually:

```bash
# Run linter and auto-fix safe issues
ruff check . --fix

# Run code formatter
ruff format .
```

### 3.2 Frappe DocType & Schema Changes

- **Developer Mode Required:** Ensure Developer Mode is enabled (`bench set-config -g developer_mode 1`) so that DocType changes in Frappe Desk export JSON files automatically into `oan_grievance_service/`.
- **JSON File Formatting:** Do not manually modify indentation or remove trailing brackets in generated DocType JSON files.

### 3.3 Patches (`patches.txt`)

- `oan_grievance_service/patches.txt` is **append-only**.
- Always add new patches at the bottom of the file on a new line.
- Never reorder or remove existing lines.

### 3.4 Markdown Documentation

- Documentation in `docs/` is formatted automatically by **Prettier** via pre-commit hooks.
- Do not manually pad markdown table cells or change heading spacing.

### 3.5 API Standards & Best Practices

- When creating or modifying REST APIs, follow the architectural conventions and decorator pipeline in [**`docs/api-development-standards.md`**](file:///Users/arnav/Code/frappe_local/frappe-bench/apps/oan_grievance_service/docs/api-development-standards.md).

---

## 4. Writing & Running Automated Tests

Every new feature, bug fix, or workflow transition **MUST** include automated test coverage.

### 4.1 Where to Place Tests

- **DocType-specific tests:** `oan_grievance_service/<module>/doctype/<doctype>/test_<doctype>.py`
- **Service/API tests:** Located alongside service modules or under test folders.

### 4.2 Writing Frappe Tests

Inherit from `frappe.tests.utils.FrappeTestCase`:

```python
import frappe
from frappe.tests.utils import FrappeTestCase

class TestGrievance(FrappeTestCase):
	def setUp(self):
		frappe.db.rollback()

	def test_grievance_submission_generates_ticket(self):
		doc = frappe.get_doc({
			"doctype": "Grievance",
			"submitter_type": "Individual Farmer",
			"submitter_name": "Test Farmer",
			"contact_mobile": "+251911000000",
			"submission_channel": "da_assisted",
			"administrative_area": "Bishoftu",
			"service_category": "Agricultural Inputs",
			"grievance_type": "Fertilizer Shortage",
			"description": "Test grievance description with minimum required length.",
		}).insert(ignore_permissions=True)

		self.assertTrue(doc.ticket_number)
		self.assertEqual(doc.status, "Submitted")
```

### 4.3 Running Tests Locally

Run the test suite inside your bench environment before pushing:

```bash
# Run all tests for the app
bench --site grievance.localhost run-tests --app oan_grievance_service

# Run tests for a specific doctype
bench --site grievance.localhost run-tests --doctype "Grievance"

# Run a specific test method
bench --site grievance.localhost run-tests --doctype "Grievance" --test test_grievance_submission_generates_ticket
```

---

## 5. Pre-PR Quality Verification Checklist

Before creating your Pull Request, execute this 4-step checklist:

### Step 1: Run Pre-Commit Checks

```bash
pre-commit run --all-files
```

_All hooks (whitespace, Ruff, Prettier, ESLint, EOF) must pass._

### Step 2: Run Semgrep Security / Correctness Scan

```bash
semgrep scan --config r/python.lang.correctness
```

### Step 3: Run Full Test Suite

```bash
bench --site grievance.localhost run-tests --app oan_grievance_service
```

_All unit and integration tests must pass._

### Step 4: Check Uncommitted / Untracked Files

```bash
git status
```

_Ensure all modified files (especially DocType JSONs and patches) are staged and no temporary debug logs or `.pyc` files are committed._

---

## 6. Raising the Pull Request

### 6.1 Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

- `feat: add SLA pause calculation on pending submitter`
- `fix: prevent duplicate ticket allocation on retry`
- `test: add unit tests for DA assisted intake`
- `docs: update intake API flow diagram`
- `chore: update pre-commit ruff version`

### 6.2 PR Description Template

When opening the PR on GitHub:

```markdown
### Summary
Brief 1-2 sentence description of what this PR introduces or resolves.

### Key Changes
- Added field `xyz` to `Grievance` DocType.
- Implemented hook guard in `before_workflow_action`.
- Added test coverage in `test_grievance.py`.

### Related Issues
Closes #123

### Verification Checklist
- [x] Branch branched from latest `develop`
- [x] Pre-commit hooks executed and passed locally (`pre-commit run --all-files`)
- [x] All automated tests passed (`bench run-tests --app oan_grievance_service`)
- [x] DocType JSON files exported cleanly (developer mode)
- [x] `patches.txt` is append-only (if applicable)
```
