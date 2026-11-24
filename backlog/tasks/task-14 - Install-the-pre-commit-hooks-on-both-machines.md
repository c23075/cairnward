---
id: TASK-14
title: Install the pre-commit hooks on both machines
status: To Do
assignee:
  - '@owner'
created_date: '2026-11-24T15:57:27Z'
updated_date: '2026-11-24T16:35:31Z'
labels:
  - repo
  - security
dependencies: []
priority: medium
type: action
ordinal: 14000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
`uv sync && uv run pre-commit install` on the desktop and the laptop. The secret scan runs only where it's installed. After PR 68 merges
<!-- SECTION:DESCRIPTION:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Was A27 in docs/plan/tasks.json, before 2026-11-25.
<!-- SECTION:NOTES:END -->
