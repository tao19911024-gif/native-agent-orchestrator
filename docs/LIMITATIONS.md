# Current limitations

Foundry is an early alpha. The initial public source retains the existing runtime
behavior. Source publication does not establish cross-platform or unattended
production readiness.

| Area | Current boundary |
| --- | --- |
| Host platform | Primary development and verification use Windows with Python 3.13. Linux and macOS are not qualified by this publication. |
| Windows layout | Some temporary-output guidance and closure paths assume D: or H:. Runtime upgrade aliases `current` and a bare version assume `D:/Foundry`; an explicit target interpreter is also supported. Release retention searches historical local roots. |
| Native providers | Codex and Claude must be installed and authenticated separately. Model access, versions and usage limits belong to the provider/account. |
| Model catalogue | New calls may query the installed native client. `PDCL_MODEL_CATALOGUE=snapshot` selects the retained offline catalogue; it does not grant model access or update prices. |
| Optional index | Index preparation currently qualifies CodeGraph 1.6.2. An unavailable index falls back to scoped direct reads. This repository does not redistribute CodeGraph. |
| Background host | Independent Windows execution uses the existing host integration. Surviving desktop exit does not imply surviving user logoff or machine reboot. |
| Completion delivery | Retained handles and evidence are supported boundaries. Automatic background delivery to the originating desktop chat depends on a host receiver and is not guaranteed by this package. |
| Tests | Some tests require an explicitly supplied native client, installed environment, Windows facilities or a private project wrapper. Offline checks do not qualify these integrations. |
| Distribution | Initial publication is source on GitHub. It is not a PyPI release or a bundled native-client distribution. |

The CLI exposes advanced recovery, role-transfer, hosted execution and runtime
upgrade commands. Their retained lifecycle requirements still apply. Do not use
the public snapshot to rewrite or move an unrelated active task's records.
