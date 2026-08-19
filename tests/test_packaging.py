"""Check the repository someone actually clones.

Every other test here exercises imported code. None of them would notice if
`install.sh` were truncated, or if a file it depends on were missing from the
tree — and that is exactly what got this plugin rejected from the marketplace:

    "the documented `install.sh --udev` path is a no-op because `install.sh`
     has no argument dispatcher and the referenced udev rule is absent"

Both were true of the pushed tree and neither was true of any file under test.
So these assertions run against the repository on disk, not against imports:
the documented commands exist, the paths referenced from scripts and docs
resolve, and the plugin manifest points at files that are really there.
"""

import json
import os
import re
import stat
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts) -> str:
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8") as fh:
        return fh.read()


class InstallScriptTests(unittest.TestCase):
    """The one script a new user is told to run."""

    def setUp(self):
        self.path = os.path.join(ROOT, "install.sh")
        self.text = read("install.sh")

    def test_it_parses(self):
        # A truncated script is still a file. It is not still valid bash.
        subprocess.run(["bash", "-n", self.path], check=True)

    def test_it_is_executable(self):
        mode = os.stat(self.path).st_mode
        self.assertTrue(mode & stat.S_IXUSR, "install.sh must be executable")

    def test_every_documented_option_is_dispatched(self):
        """The header comment and the case statement must not drift apart.

        The rejected build documented --udev in its header and had no case
        statement at all, so the flag was silently ignored.
        """
        documented = set(re.findall(r"^#\s+(--[a-z]+)", self.text, re.M))
        self.assertTrue(documented, "no options documented in the header")

        case_body = self.text.split('case "${1:-}" in', 1)
        self.assertEqual(len(case_body), 2, "install.sh has no argument dispatcher")
        handled = set(re.findall(r"^\s+(--[a-z|-]+)\)", case_body[1], re.M))
        handled = {opt for branch in handled for opt in branch.split("|")}

        missing = documented - handled
        self.assertFalse(missing, f"documented but not handled: {sorted(missing)}")

    def test_unknown_options_are_rejected(self):
        result = subprocess.run(
            ["bash", self.path, "--definitely-not-an-option"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_help_works_without_touching_anything(self):
        result = subprocess.run(
            ["bash", self.path, "--help"], capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--udev", result.stdout)

    def test_the_udev_rule_is_inline_and_scoped_to_our_vendor(self):
        """No external rules file to go missing, and no blanket hidraw grant.

        Inline because the previous version copied a file out of install/ that
        was not in the pushed tree. Scoped because a rule matching every hidraw
        node would hand this plugin's users far more access than it needs.
        """
        self.assertIn("udev_rule_text()", self.text)
        self.assertIn('KERNEL=="hidraw*"', self.text)
        self.assertIn('ATTRS{idVendor}=="$VENDOR_ID"', self.text)
        self.assertIn('TAG+="uaccess"', self.text)

    def test_it_reads_no_file_it_does_not_ship(self):
        """Any $REPO_DIR/... path in the script must exist in the tree."""
        for ref in re.findall(r'\$REPO_DIR/([A-Za-z0-9_./-]+)', self.text):
            self.assertTrue(
                os.path.exists(os.path.join(ROOT, ref)),
                f"install.sh refers to {ref}, which is not in the repository",
            )


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(read("manifest.json"))

    def test_required_fields_are_present(self):
        for key in ("schemaVersion", "id", "name", "version", "author",
                    "license", "description", "kinds", "entryPoints"):
            self.assertIn(key, self.manifest)

    def test_entry_points_resolve(self):
        for name, target in self.manifest["entryPoints"].items():
            self.assertTrue(
                os.path.exists(os.path.join(ROOT, target)),
                f"entryPoint {name} -> {target} does not exist",
            )

    def test_version_is_a_release_number(self):
        self.assertRegex(self.manifest["version"], r"^\d+\.\d+\.\d+$")

    def test_the_cli_reports_the_manifest_version(self):
        """One version number, everywhere it is shown.

        These were a literal in hskctl/__init__.py and a field in the manifest,
        and they drifted: the manifest said 1.0.1 while `hskctl --version`
        still claimed 0.1.0. The panel now shows this number too, so a wrong
        one is worse than none -- it is what someone reports a bug against.
        """
        from hskctl import __version__
        self.assertEqual(__version__, self.manifest["version"])

    def test_the_cli_prints_it(self):
        result = subprocess.run(
            [os.path.join(ROOT, "bin", "hskctl"), "--version"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertIn(self.manifest["version"], result.stdout + result.stderr)

    def test_settings_schema_matches_its_defaults(self):
        widget = self.manifest["barWidget"]
        keys = {entry["key"] for entry in widget["schema"]}
        self.assertEqual(keys, set(widget["defaults"]))


class ListingTests(unittest.TestCase):
    """What the marketplace and a browsing human look for."""

    def test_the_files_a_listing_needs_exist(self):
        for name in ("README.md", "LICENSE", "manifest.json", "preview.png"):
            self.assertTrue(os.path.exists(os.path.join(ROOT, name)), name)

    def test_the_bundled_cli_is_executable(self):
        launcher = os.path.join(ROOT, "bin", "hskctl")
        self.assertTrue(os.stat(launcher).st_mode & stat.S_IXUSR)

    def test_readme_links_resolve(self):
        """A dead link in the README is the first thing a reviewer clicks."""
        readme = read("README.md")
        for target in re.findall(r"\]\((?!https?:)([^)#]+)\)", readme):
            self.assertTrue(
                os.path.exists(os.path.join(ROOT, target.strip())),
                f"README links to {target}, which does not exist",
            )

    def test_readme_only_shows_commands_install_sh_accepts(self):
        readme = read("README.md")
        script = read("install.sh")
        for flag in set(re.findall(r"install\.sh (--[a-z]+)", readme)):
            self.assertIn(
                f"  {flag})", script,
                f"README tells people to run 'install.sh {flag}', "
                f"which the script does not handle",
            )


if __name__ == "__main__":
    unittest.main()
