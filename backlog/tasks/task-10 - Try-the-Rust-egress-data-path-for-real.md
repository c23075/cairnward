---
id: TASK-10
title: Try the Rust egress data path for real
status: To Do
assignee:
  - '@owner'
created_date: '2026-11-24T15:57:26Z'
updated_date: '2026-11-24T15:58:14Z'
labels: []
dependencies: []
priority: medium
type: action
ordinal: 10000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Set `[egress] implementation = "rust"` with your real agents and the sites they use, TLS inspection included (build with `cargo build --release --manifest-path rust/Cargo.toml`). ADR-0020 switches the default only after a real run; it's only been tested against local stand-ins
<!-- SECTION:DESCRIPTION:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Was A18 in docs/plan/tasks.json, before 2026-11-25.
<!-- SECTION:NOTES:END -->
