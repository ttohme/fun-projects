# Personal AI assistant repository rules

## Mission
This repo runs intake, task routing, personal automation, and safe coding workflows.

## Core rules
- Todoist is the source of truth for tasks.
- Never send outbound email, delete files, delete tasks, or call home-control write actions without an approval step.
- Prefer deterministic workflow nodes over agent improvisation for Gmail, Todoist, Syncthing, and Home Assistant.
- For code changes, work in a disposable branch or worktree.
- For dependency installs or unknown repos, use the sandbox runner.

## Required checks
- Run unit tests for touched modules.
- Validate JSON/YAML before write.
- If a workflow changes task semantics, update `/evals` with a regression case.
