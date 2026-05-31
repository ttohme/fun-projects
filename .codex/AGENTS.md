# Codex-specific working agreements

- Use plan/approval mode for any shell command that changes system state.
- Before editing workflow files, inspect `apps/orchestrator/workflows/` and `db/schema.sql`.
- Never embed secrets in code or config examples.
- Prefer small diffs and explain rollback steps.
