"""Test-only A1 profiles: generated from the invented OrcaSlicer resource tree in fixtures/orca_resources.

Print Buddy ships no A1 profiles; production generates them from the operator's own OrcaSlicer and trusts only
the identities in facts.py. Tests generate them from the fixture tree instead and trust its hashes, for the test
process only. Nothing here is OrcaSlicer or Bambu Lab material (see fixtures/orca_resources/README.md).
"""
import tempfile
from pathlib import Path

from print_buddy.printers.bambulab import facts, profiles
from print_buddy.printers.bambulab.a1 import A1Adapter

RESOURCES = Path(__file__).resolve().parent / "fixtures" / "orca_resources"
# A profile folder for tests that load settings themselves: <HERMES>/state/orca-profiles/bambulab_a1.
HERMES = Path(tempfile.mkdtemp(prefix="a1-test-profile-"))


def identity(resources=RESOURCES):
    upstream, version = profiles.flatten(resources, facts.A1["orca_profiles"].value)
    return {"label": "Print Buddy test fixture (not OrcaSlicer)", "vendor_version": version,
            "sha256": {kind: profiles.sha256(upstream[kind]) for kind in profiles.KINDS}}


def fixture_facts(resources=RESOURCES):
    """facts.A1 with the fixture tree as the only trusted identity."""
    return {**facts.A1, "orca_identity": facts.Fact((identity(resources),), "TEST", False)}


def trust(setattr=None, resources=RESOURCES):
    """Trust the fixture identity: through monkeypatch.setattr when given, else for the whole process."""
    value = fixture_facts(resources)
    if setattr is None:
        A1Adapter.facts = value
    else:
        setattr(A1Adapter, "facts", value)


def install(adapter, resources=RESOURCES):
    """Generate the fixture templates into the adapter's profile folder; returns the folder."""
    profiles.install(adapter, resources)
    return adapter.pinned_templates_folder()
