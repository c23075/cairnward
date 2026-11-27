---
id: TASK-68
title: Threat-model MCP Apps and elicitation
status: To Do
assignee:
  - '@agent'
created_date: '2026-11-25T07:45:31Z'
labels:
  - security
  - clients
dependencies: []
priority: low
type: work
ordinal: 68000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
MCP Apps (the io.modelcontextprotocol/ui extension, stable 2026-01-26) lets an MCP tool point to a ui:// HTML page that the chat app runs in a sandboxed frame; elicitation lets an MCP server ask the user for input through a form (non-sensitive data only) or a URL. Cairnward's MCP proxy passes no resources, so these pages never show, and it refuses elicitation on purpose (src/cairnward/mcp/upstream.py). Before letting either through, add threat-model entries: third-party page code in the client, page-started tool calls, the page's declared CSP domains against egress policy, and forms that ask for secrets. Record the decision in the spec. Wishlist: Interfaces, Interactive views in chat.
<!-- SECTION:DESCRIPTION:END -->
