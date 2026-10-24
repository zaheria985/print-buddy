# Contributing to Print Buddy

Thanks for helping improve Print Buddy. This guide describes the repository workflow and project specific checks. The [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md) provides the expected pull request description structure.

## Before starting

- Search open issues and pull requests for related work. For larger changes, discuss the approach in an issue first so scope and safety expectations are clear.
- Read the relevant project documentation and code. The README describes supported printers and workflows; configuration details are in `docs/CONFIGURATION-YAML.md`.
- Keep changes focused. Separate unrelated fixes and avoid including machine specific settings, credentials, private printer data, or personal model files.
- Printer control is safety sensitive. Keep offline analysis and tests separate from live printer operations. A code change or successful test does not authorize contacting or operating a printer.

## Prepare a change

- Use a branch for your change and keep commits focused and clearly named.
- Add or update tests for changed behavior. For a bug fix, include a regression test where practical.
- Follow the existing Python and configuration patterns. Do not weaken printer readiness, artifact validation, bed bounds, or confirmation gates to make a test pass.
- For public model fixtures, use only redistributable models and record exact provenance and license in the case manifest. Keep private or non redistributable files outside the repository.
- Update documentation when user visible behavior, setup, configuration, or supported capabilities change. Do not claim support beyond what code and tests establish.

## Run checks

Use Python 3.11 or newer. From the repository root, create an isolated environment, install test dependencies and run the offline suite:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-test.txt
python -m pytest -q tests
```

Relevant focused checks can be run individually. For example:

```bash
python3 -m pytest -q tests/corpus
python3 -m pytest -q tests/slicer
```

Real slicer checks require a compatible OrcaSlicer installation and are enabled with `PRINT_BUDDY_REAL_SLICER=1`. They invoke the slicer locally; they are not printer tests. The Discord flow corpus uses fake interactions and does not contact a printer. Check the CI workflow for the current Python versions and additional integration jobs.

Report which checks you actually ran and their result in the pull request. Clearly identify checks not run and why. Do not describe a pending CI job as passing.

## Open a pull request

- Open a pull request against `main` and use the repository pull request template.
- Explain the user impact, verified cause when fixing a bug, the proposed behavior, and relevant risks or limitations.
- Link related issues. Use `Fixes #N` only when the pull request is intended to close that issue after merge.
- Include test results and distinguish automated tests from manual or live checks. Do not include secrets or personal information in examples or logs.
- Keep the PR in draft while implementation or required checks are incomplete. Respond to review feedback with focused updates.

## Documentation and release notes

Use [the documentation home](docs/README.md) for user guidance. Keep the root README focused on purpose, compatibility and the first setup step; put detailed reference material in `docs/`. Check relative links from the file that contains them. Follow [the release-note guide](docs/RELEASE-NOTES.md): user capabilities first, fixes next, implementation details and PR references last. Keep pending changes separate from shipped releases, and preserve the detailed internal history.

## Automated PR review support

The **Contribution checks / contribution-policy** check runs when a PR opens,
changes, is reopened or has its description edited, including fork PRs. It checks:

1. A written summary and actual verification evidence (or explicit unrun checks).
2. `Documentation: updated` with a changed documentation file, or `not needed`
   with a specific reason. User-facing features, changed defaults, settings,
   installation steps and supported integrations should update their relevant
   guide in the same PR. Maintainers review exemptions; a checkbox cannot prove
   documentation quality.
3. `Wiki: updated`, `not applicable` or `deferred`, with a reason. An update
   needs a wiki page URL; a deferral needs a GitHub issue URL. The versioned
   [documentation home](docs/README.md) is authoritative. Do not duplicate
   entire guides into a separate wiki. If a wiki entry exists, check its links
   and summary against the feature; otherwise say why it is not applicable.
4. Local Markdown file destinations in changed Markdown files. External URLs,
   heading anchors and semantic accuracy remain human review responsibilities.

Fill the `Documentation impact` fields in the PR template literally. For example:

```text
Documentation: updated
Reason: Added the new setting and its default to docs/CONFIGURATION-YAML.md.
Wiki: not applicable
Wiki reason: Versioned docs are authoritative; no separate wiki page describes this setting.
```

Existing PRs can add this section when revising their descriptions. The check
is read-only: no auto-merge, auto-close, code execution with a write token,
contributor comments, labels, wiki writes or publication. Existing owner
assignment automation remains separate. Branch protection currently requires
other CI checks; this new check does not silently change repository protection.
Maintainers must review its result before merge until it is made required.

For local reproduction, save JSON containing `body` (PR description) and `files`
(changed repository-relative paths), then run:

```bash
python scripts/check_contribution.py --metadata /path/to/pr.json
python -B -m unittest discover -s tests -p test_contribution_policy.py
```

### Review checklist

- Confirm scope and related work before implementation; open a draft PR early.
- Check user-facing guides, configuration examples and wiki impact for each feature.
- Verify regression coverage and exact-head CI, without treating offline tests as hardware evidence.
- Check compatibility, rollback, safety guards and third-party attribution.
- Keep pending work and release publication separate from merged source.
