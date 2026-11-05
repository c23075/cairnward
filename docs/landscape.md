# Agent security and privacy: the state of the industry

Written 2026-11-05. This page goes out of date quickly, so check its date before relying on it.

This is desk research from web searches, not testing. Most sources are vendors, law firms and
preprints. Individual CVEs weren't checked against NVD or vendor advisories, and the regulators' own
texts weren't read. Check a figure at its source before citing it. Two widely repeated claims were
left out because they couldn't be traced to a source: a "340% surge" in prompt injection and a
"$4.7M average breach cost".

## Summary

Agents are being deployed faster than they're being secured, and prompt injection is still unsolved.
The response is moving towards limiting what a compromised agent can do, rather than trying to make
agents impossible to fool. Standards and regulation are arriving, but most of what exists is
voluntary or early.

## Adoption is ahead of controls

The clearest snapshot found is [Gravitee's survey](https://www.gravitee.io/state-of-ai-agent-security)
of 750 senior technology leaders in the UK and US, from April 2026. Gravitee sells agent management
software and discloses little of its method, so treat these numbers as directional.

- **Growth:** the number of agents roughly doubled in four months, and about 38% of organisations
  report more than 100.
- **Governance:** 19.7% say all their agents are fully secured and governed before going live.
- **Monitoring:** coverage averages about 52% of agents, while 91.8% say they're confident in their
  visibility.
- **Incidents:** 54% experienced or suspected an agent security or privacy incident in the past year,
  and 34.9% confirmed one.

## Prompt injection is still unsolved

- **Attacks are real.** The [Cloud Security Alliance](https://labs.cloudsecurityalliance.org/research/csa-research-note-indirect-prompt-injection-in-the-wild-2026/)
  describes indirect injection being used in the wild, including hidden instructions aimed at AI
  inbox assistants.
- **Defences inside the model keep failing.** A [2026 study](https://www.alphaxiv.org/abs/2604.23887)
  tested nine defence configurations against an adaptive attacker, and every one that relied on the
  model to protect itself eventually broke. An [earlier evaluation](https://arxiv.org/pdf/2505.18333)
  also found defences weaker than reported.
- **Defences outside the model are better, but not proven.** CaMeL, Progent and similar designs
  enforce rules outside the model. A [June 2026 preprint](https://arxiv.org/abs/2606.26479) says
  adaptive attacks broke twelve such designs more than 90% of the time, yet Progent held up in its
  own reproduction. The evidence is mixed.
- **Governments agree.** According to [Sysdig](https://www.sysdig.com/learn-cloud-native/prompt-injection),
  Five Eyes agencies said in May 2026 that no single safeguard is enough. This is secondhand.

## The plumbing is a major attack surface

[One analysis](https://host0.ai/blog/state-of-ai-agent-security-2026) argues agent security is now a
supply-chain problem first and a prompt injection problem second. These cases are as reported, and
some sources disagree on the details.

- **MCP servers:** [one source](https://www.practical-devsecops.com/mcp-security-statistics-2026-report/)
  counts more than 30 CVEs in 60 days, about 43% of them command injection. nginx-ui
  (CVE-2026-33032) was reportedly exploited in the wild.
- **MCP's design:** [OX Security](https://labs.cloudsecurityalliance.org/research/csa-research-note-mcp-by-design-rce-ox-security-20260420-csa/)
  reported systemic remote code execution in Anthropic's MCP SDK, which it attributes to a deliberate
  design choice.
- **Coding agents:** reported flaws affect Cursor, Windsurf and Claude Code, including hooks and MCP
  servers that run before the trust dialog.
- **Agent runtimes:** [four chained OpenClaw CVEs](https://labs.cloudsecurityalliance.org/research/csa-research-note-openclaw-claw-chain-sandbox-escape-2026051/)
  reportedly let an attacker get from inside the sandbox to the gateway and host.
- **Underreporting:** OWASP's Q1 2026 report, as cited in the search results, found that one of eight
  major AI incidents received a CVE. CVE counts understate the problem.

## Standards are voluntary so far

- **OWASP:** the [Top 10 for Agentic Applications](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026)
  (December 2025) is the common reference, and has no legal force of its own.
- **NIST:** its [AI Agent Standards Initiative](https://www.nist.gov/caisi/ai-agent-standards-initiative)
  started in February 2026, along with a concept paper on agent identity. No finished guidance from
  it was found. [Pillsbury](https://www.pillsburylaw.com/en/news-and-insights/nist-ai-agent-standards.html)
  notes that these frameworks aren't binding.

## Privacy regulation mostly applies existing law

- **EU AI Act:** the AI Omnibus [entered into force on 27 July 2026](https://www.cuatrecasas.com/en/spain/intellectual-property/art/digital-omnibus-ai-has-been-published).
  [High-risk obligations move](https://bratby.law/digital-omnibus-ai-high-risk-delay/) to
  2 December 2027 for stand-alone systems and 2 August 2028 for AI in regulated products. GDPR still
  applies alongside it.
- **Spain:** the AEPD's [guidance of February 2026](https://www.insideprivacy.com/artificial-intelligence/spanish-supervisory-authority-issues-detailed-guidance-on-agentic-ai-and-gdpr-compliance/)
  treats agents as technical means of processing rather than legal actors, so responsibility stays
  with the organisations using them.
- **Elsewhere:** [Turkey](https://www.mondaq.com/turkey/new-technology/1764108/approaches-of-turkish-and-international-authorities-to-agentic-ai)
  and Hong Kong have issued guidance, the Dutch regulator has warned about broad system access, and
  the UK ICO has published early thinking.

## Where it seems to be heading

This section is a judgement, not a finding.

- **Containment over detection:** sandboxing, least privilege and enforcement outside the model are
  becoming the default advice.
- **Agents as identities:** agents are increasingly treated as accounts that need their own
  credentials and a named owner, but this work is immature.
- **Gateways as control points:** vendors push these hard, partly because they sell them, so the
  claim deserves some scepticism. That applies to this project's approach too.
- **Still open:** approval fatigue, data leaving through approved channels, and agents chaining
  permissions they were each legitimately given.

## What this means for Cairnward

Cairnward takes the containment approach: the model and its clients are untrusted, and enforcement
sits outside them. That doesn't show it works, because it hasn't had an independent security audit.
See the [threat model](threat-model.md) for the threats named here.

| Industry problem | In Cairnward | Threats |
|---|---|---|
| Prompt injection | Limits the damage, doesn't prevent it. An accepted risk | T-07, T-14 |
| MCP tools changing after approval | Pinning, and disabling on change | T-04, T-22 |
| Data leaving through approved channels | **Open:** HTTPS to an allowed host can't be read unless TLS inspection is on | T-17, T-23 |
| Approval fatigue | **Open:** a user can approve without reading. Risk tiers keep prompts rare | T-14 |
| Supply chain | Hash-locked dependencies. The signed release workflow has never run on GitHub | T-16 |

The threat model maps its controls to the OWASP Top 10 for LLM Applications (2025), not yet to the
Top 10 for Agentic Applications. How the AI Act or GDPR applies to Cairnward or its users hasn't been
assessed, and nothing on this page is legal advice.
