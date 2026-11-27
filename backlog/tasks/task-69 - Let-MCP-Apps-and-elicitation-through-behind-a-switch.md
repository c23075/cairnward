---
id: TASK-69
title: Let MCP Apps and elicitation through, behind a switch
status: To Do
assignee:
  - '@agent'
created_date: '2026-11-25T07:45:35Z'
labels:
  - clients
  - security
dependencies:
  - TASK-68
type: work
ordinal: 69000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
After TASK-68. A switch (D55), off by default, audited and shown as a privacy warning when on. With it on: pass resources/list and resources/read through for ui:// pages; pin each page by hash, as tool definitions are; hold the page's declared CSP domains to egress policy; audit tool calls a page starts (as digests); show elicitation forms in Cairnward's dashboard and refuse any form that asks for a secret. Add a test against a real MCP server that serves an MCP App (testing lessons in AGENTS.md).
<!-- SECTION:DESCRIPTION:END -->
