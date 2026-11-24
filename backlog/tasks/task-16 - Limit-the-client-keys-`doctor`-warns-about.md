---
id: TASK-16
title: Limit the client keys `doctor` warns about
status: To Do
assignee:
  - '@owner'
created_date: '2026-11-24T15:57:27Z'
updated_date: '2026-11-24T15:58:15Z'
labels: []
dependencies: []
priority: medium
type: action
ordinal: 16000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Run `cairnward doctor`, then set `environments = [...]` in `clients.toml`, or rerun `cairnward client add <name> --environment <env>`. Keys made before D86 can use any environment, including `scratch`, which is more open than `coding` (I39)
<!-- SECTION:DESCRIPTION:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Was A31 in docs/plan/tasks.json, before 2026-11-25.
<!-- SECTION:NOTES:END -->
