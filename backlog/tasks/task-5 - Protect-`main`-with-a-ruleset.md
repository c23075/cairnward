---
id: TASK-5
title: Protect `main` with a ruleset
status: To Do
assignee:
  - '@owner'
created_date: '2026-11-24T15:57:25Z'
updated_date: '2026-11-24T16:35:29Z'
labels:
  - repo
milestone: m-0
dependencies: []
priority: high
type: action
ordinal: 5000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Require a pull request and the CI checks; block force pushes and deletion. Nothing stops a merge before CI passes; PRs 22 to 24 broke `main` (I25). Use a ruleset, not classic protection, so Scorecard can read it ([steps](/docs/repository-management.md#1-protect-the-default-branch))
<!-- SECTION:DESCRIPTION:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Was A21 in docs/plan/tasks.json, before 2026-11-25.
<!-- SECTION:NOTES:END -->
