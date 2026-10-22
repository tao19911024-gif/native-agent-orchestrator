# Third-party dependencies and native clients

The MIT license in this repository covers Native Agent Orchestrator's source and
documentation.
It does not relicense independently installed dependencies or native clients.
No third-party dependency source, wheel cache, native executable, or model weight
is bundled in this source repository.

## Declared Python dependencies

| Dependency | Declared version | License identified in installed package metadata |
| --- | --- | --- |
| langgraph | 1.2.11 | MIT |
| langgraph-checkpoint-sqlite | 3.1.1 | MIT |
| pytest, optional test extra | 9.1.1 | MIT |
| setuptools, build requirement | 77 or newer | MIT |

Transitive packages retain their upstream licenses. The inspected development
environment also includes BSD, Apache-2.0, PSF-2.0 and MPL-2.0 components, including
certifi and components of orjson. These dependencies are installed independently;
their licenses and notices remain in their distributions. A future bundled
binary distribution would need its own complete license and source-obligation
review rather than relying on this source-only publication.

## External tools

- Codex: independently installed native client; upstream source is available at
  <https://github.com/openai/codex>. OpenAI service access follows the applicable
  account and service terms.
- Claude Code: independently installed native client under Anthropic's terms.
- CodeGraph: optional separately installed index integration, currently qualified
  at version 1.6.2.

Native Agent Orchestrator does not redistribute these executables, supply credentials,
or grant access to their services. Project and product names identify integrations;
they do not imply endorsement or an affiliation with their vendors.
