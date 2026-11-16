# Contributing

## Workflow

1. Fork the repo and create a feature branch.
2. Make changes following existing conventions (PEP 8, Frappe style).
3. Add/update tests.
4. Run lint + tests locally.
5. Open a PR with a clear description.

## Code conventions

- Python: PEP 8, 4-space indent, type hints where helpful.
- Frappe: use `frappe.get_doc`, `frappe.db`, `frappe.get_all`; avoid raw SQL.
- Keep hook functions thin; move logic into module-level helpers.
- Do not mutate core ERPNext code – use `doc_events` / scheduler hooks.
- No hardcoded secrets; use site config / environment variables.

## Commit messages

Conventional commits: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`.

## Pull requests

- One logical change per PR.
- Include test evidence.
- Update docs when behavior changes.

## Reporting issues

Open an issue with reproduction steps, expected vs actual behavior, and Frappe/ERPNext versions.
" filepath="C:\Users\lenovo\Downloads\Sales Commission Management for ERPNext\sales_commission_management\docs\contributing.md"