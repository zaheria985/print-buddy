"""Offline regression coverage for PR documentation policy."""
import importlib.util
import unittest
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_contribution.py"

class PolicyTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("policy", SCRIPT)
        self.policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.policy)

    def body(self):
        return """## Summary and user impact
A new option for operators.
## Verification and test results
Ran the focused offline tests: 4 passed.
## Documentation impact
Documentation: updated
Reason: New configuration option documented in docs/CONFIGURATION-YAML.md.
Wiki: not applicable
Wiki reason: Versioned docs are authoritative; no separate wiki page exists.
"""

    def test_markdown_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("[ok](guide.md) [remote](https://example.com) [anchor](#heading)")
            (root / "guide.md").write_text("# Guide")
            self.assertEqual([], self.policy.check_links(root, ["README.md", "deleted.md"]))
            (root / "README.md").write_text("[bad](missing.md) [escape](../outside.md)")
            self.assertEqual(2, len(self.policy.check_links(root, ["README.md"])))

    def test_valid_feature(self):
        self.assertEqual([], self.policy.check_pr(self.body(), ["print_buddy/settings.py", "docs/CONFIGURATION-YAML.md"]))

    def test_feature_without_docs_requires_reasoned_exemption(self):
        body = self.body().replace("Documentation: updated", "Documentation: not needed").replace("New configuration option documented in docs/CONFIGURATION-YAML.md.", "Internal rename only; operator behavior and settings are unchanged.")
        self.assertEqual([], self.policy.check_pr(body, ["print_buddy/core/internal.py"]))

    def test_updated_requires_changed_docs(self):
        self.assertTrue(self.policy.check_pr(self.body(), ["print_buddy/settings.py"]))

    def test_template_comments_do_not_count(self):
        self.assertTrue(self.policy.check_pr("<!-- " + self.body() + " -->", ["README.md"]))

    def test_wiki_evidence_required(self):
        self.assertTrue(self.policy.check_pr(self.body().replace("Wiki: not applicable", "Wiki: updated"), ["docs/README.md"]))

    def test_placeholder_rejected(self):
        self.assertTrue(self.policy.check_pr(self.body().replace("Reason: New configuration option documented in docs/CONFIGURATION-YAML.md.", "Reason: TODO"), ["docs/README.md"]))

    def test_missing_verification_rejected(self):
        self.assertTrue(self.policy.check_pr(self.body().replace("Ran the focused offline tests: 4 passed.", ""), ["docs/README.md"]))

    def test_deferred_wiki_needs_issue_link(self):
        body = self.body().replace("Wiki: not applicable", "Wiki: deferred")
        self.assertTrue(self.policy.check_pr(body, ["docs/README.md"]))
        body += "\nhttps://github.com/catduckgnaf/print-buddy/issues/120\n"
        self.assertEqual([], self.policy.check_pr(body, ["docs/README.md"]))

if __name__ == "__main__":
    unittest.main()
