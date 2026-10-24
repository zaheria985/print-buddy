# Print Buddy documentation

[Project overview](../README.md) · [Contributing](../CONTRIBUTING.md)

## 1. Install one mode

- [Standalone bot](STANDALONE.md): Discord without Hermes or a required LLM; token permissions, configuration, setup checks and systemd services.
- [Hermes installation](INSTALL.md): profile installation, private overlays and listener setup.
- [Docker](DOCKER.md): container configuration, persistent storage and service lifecycle. Follow the guide for the checked-out revision; do not assume a draft packaging PR is available.

Do not run both Discord modes with the same token. Installing, enabling services and contacting a printer are separate actions from offline model analysis.

## 2. Configure and use

- [YAML configuration and TOML migration](CONFIGURATION-YAML.md): schema, precedence and safety limits.
- [Detailed project reference](PROJECT-REFERENCE.md): workflow examples, commands, printer models, spool identity rules, monitoring and updates.
- [Printer profiles and offline delivery](PRINTER-PROFILES.md): vendor/model inheritance, supported delivery methods and verification limitations.
- [AD5X integration contract](AD5X-CONTRACT.md): printer-specific behavior and limitations.
- [OctoPrint/Marlin notes](OCTOPRINT-MARLIN.md): read the integration-specific limits before assuming Moonraker feature parity.

## 3. Troubleshoot by symptom

| Symptom | First check |
| --- | --- |
| Bot replies twice | Confirm only one mode uses the token; see [standalone setup](STANDALONE.md). |
| Startup rejects a setting | Read the named key and [configuration precedence](CONFIGURATION-YAML.md); YAML takes precedence over corresponding TOML files. |
| Slicing fails or model does not fit | Check slicer command, selected printer/profile and model bounds in the [reference](PROJECT-REFERENCE.md). Do not bypass G-code or clearance guards. |
| Spool or color disagrees with the printer | Read the reference's spool/IFS source-of-truth rules before rebinding slots. |
| Installation or update fails | Use the guide for your selected mode; collect the actual error without tokens, private model files or personal service addresses. |

`python3 -m print_buddy.doctor` checks setup and can contact configured Moonraker and Spoolman services. Run it only where those contacts are authorized. A clean check does not prove physical readiness.

## 4. Develop and review

- [Contribution workflow](../CONTRIBUTING.md) and [pull request template](../.github/PULL_REQUEST_TEMPLATE.md).
- [Release-note guidance](RELEASE-NOTES.md) and [internal release history](INTERNAL_RELEASE_NOTES.md).
- [Rename migration](RENAME-MIGRATION.md) for older installations.
- [CAD evaluation](KICAD-EVALUATION.md) and [reuse map](REUSE-MAP.md) for technical background, not promises of shipped capability.

These versioned repository pages are the documentation home. Update them with behavior changes so readers can use the documentation for their checked-out revision.

## Documentation ownership and wiki policy

This `docs/` directory is the dedicated, versioned documentation location for
issue #120, not a placeholder for a future website. Installation, configuration,
workflow reference and troubleshooting live here so changes can be reviewed
with code. GitHub renders these pages directly; use the docs for your selected
revision rather than assuming main describes an installed release.

Every feature PR must identify its documentation and wiki impact using the
[contribution checklist](../CONTRIBUTING.md#automated-pr-review-support).
Update the relevant guide and this index when adding a new page. A separate
GitHub Wiki, if maintained, should provide links or summaries pointing here;
it is not a second source of configuration or safety instructions. No wiki
publication or synchronization is automated by the PR checks.
