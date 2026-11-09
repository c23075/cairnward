# Cairnward documentation

Grouped by what you're here to do. Start with the [overview](overview.md) if you're new.

## Using it
- [Getting started](getting-started.md): install, choose a privacy preset, connect a client
- [Clients](clients/): setup for each tested chat app and coding agent ([checklist](clients/checklist.md) for trying a new one), [MCP](clients/mcp.md), [running clients in containers](clients/containers.md)
- [Operations](operations.md): running it day to day
- [WSL2](wsl2.md): Windows through WSL2
- [Plugins](plugins.md): the plugin contract, for MCP servers built for Cairnward

## Evaluating it
- [Overview](overview.md): what it is and where it sits in an agentic system stack
- [Status](status.md): where things stand, open issues, what's tested for real and what only against stand-ins
- [Limitations](limitations.md): what the protections don't do, in one place
- [Agent security and privacy today](landscape.md): the state of the industry, and how Cairnward compares
- [Governance for regulated companies](compliance-governance.md): using it as one layer of governance, and what a company must add
- [Benchmarks](bench/): speed measurements and gateway overhead
- [CHANGELOG](../CHANGELOG.md): what changed in each release

## Design
- [Specification](spec.md): the design, its decisions (D1–D78) and current behaviour
- [Requirements](requirements.md), with their status in [plan/requirements-status.md](plan/requirements-status.md)
- [Architecture decisions](adr/): one ADR per significant decision
- [Architecture in UML](architecture-uml.md): deployment, classes, request sequences and vault states
- [Threat model](threat-model.md): STRIDE and LINDDUN threats, controls, OWASP mapping; reviews in [reviews/](reviews/)

## Contributing and planning
- [CONTRIBUTING](../CONTRIBUTING.md) and [SECURITY](../SECURITY.md)
- [Managing the repository](repository-management.md): branches, PRs, CI, releases
- [Plans](plan/): milestone plans, the newest handoff (current state and next steps), and the [enterprise proof of concept, LLMOps and model risk management](plan/enterprise-poc.md) plan
- [Reviews](reviews/): threat-model and design reviews (NFR-S2)
