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


class BarWidgetTests(unittest.TestCase):
    """The bar item can only draw what the host component actually renders.

    `BarIconButton` inherits `text` from `WidgetButton` and then does two
    things with it: it sets `labelVisible: false`, hiding the Text that would
    have drawn it, and it routes `text` into an `OpticalGlyph` marked
    `visible: iconComponent === null`. So the moment a widget supplies its own
    `iconComponent` -- which this one must, to draw a battery glyph whose
    colour tracks the charge -- `text` renders nowhere.

    Nothing warns. The property exists, the assignment binds, and the label is
    simply absent, which is indistinguishable from the `showBatteryLabel`
    setting being off. It cost a round trip with a user toggling that setting
    both ways and seeing no difference, because there was no difference.

    Any label this widget wants belongs inside `iconComponent`.
    """

    def setUp(self):
        self.panel = read("Panel.qml")

    def bar_button_block(self):
        """The body of the BarIconButton declaration, brace-matched."""
        start = self.panel.index("BarIconButton {")
        depth, i = 0, self.panel.index("{", start)
        for j in range(i, len(self.panel)):
            if self.panel[j] == "{":
                depth += 1
            elif self.panel[j] == "}":
                depth -= 1
                if depth == 0:
                    return self.panel[i + 1:j]
        self.fail("BarIconButton block is unbalanced")

    def test_it_does_not_set_a_text_that_cannot_render(self):
        block = self.bar_button_block()
        self.assertIn("iconComponent", block)

        # Only assignments at the top level of the declaration: `text:` deeper
        # in is on a Text element inside the icon, which is the correct place.
        depth = 0
        for line in block.splitlines():
            stripped = line.strip()
            if depth == 0 and re.match(r"text\s*:", stripped):
                self.fail(
                    "Panel.qml sets `text` on BarIconButton while supplying an "
                    "iconComponent. BarIconButton hides its own label and its "
                    "glyph, so this draws nothing -- put the label inside the "
                    "iconComponent instead."
                )
            depth += line.count("{") - line.count("}")

    def test_the_battery_label_is_drawn_inside_the_icon(self):
        block = self.bar_button_block()
        icon = block[block.index("iconComponent"):]
        self.assertIn(
            "barLabelText", icon,
            "the bar label is not rendered inside iconComponent, so it cannot "
            "appear at all",
        )

    def test_the_slot_is_widened_for_the_label(self):
        """A fixed-width slot plus a wider label means overlapping neighbours.

        `slotSize` becomes `fixedWidth`, so the button does not grow to fit its
        contents. It also must not use `statusSlot`, which is *narrower* than
        `iconSlot` (21 against 27) and was picked here on the assumption that
        the name meant "a slot with a status in it".
        """
        block = self.bar_button_block()
        slot = re.search(r"slotSize:(.*?)(?=\n\s+[a-zA-Z]+:)", block, re.S)
        self.assertIsNotNone(slot, "BarIconButton does not set slotSize")
        self.assertIn("barLabelMetrics", slot.group(1))
        self.assertNotIn("statusSlot", slot.group(1))


class PluginIdConsistencyTests(unittest.TestCase):
    """The plugin id in manifest.json is the only one, everywhere.

    The id was changed in `manifest.json` alone -- one file, edited in the
    GitHub web UI -- which left `Panel.qml` registering the old module and IPC
    target, `install.sh` installing into a directory the shell would never look
    in, and every command in the README naming a plugin that does not exist.
    The whole suite passed, because nothing compared the manifest against the
    files that have to agree with it.

    This is the same failure that got the first submission rejected: a
    single-file edit leaving the documented path broken. It is not a mistake
    worth making twice.
    """

    def setUp(self):
        self.plugin_id = json.loads(read("manifest.json"))["id"]

    def looks_like_a_plugin_id(self, text):
        """Dotted tokens carrying this author's namespace.

        Keyed on the namespace rather than the plugin name, because that is
        what distinguishes an id from ordinary code: `io.github.keasbeexd.hsk`
        is one, `root.hskctl` is a QML property reference. An exact search for
        the *current* id would be useless here -- it can only ever find the
        occurrences that are already right.
        """
        namespace = re.escape(self.plugin_id.split(".")[0])
        pattern = rf"\b[A-Za-z0-9.-]*{namespace}[A-Za-z0-9.-]*\b"
        return {t.strip(".") for t in re.findall(pattern, text) if "." in t}

    def test_the_qml_registers_the_manifest_id(self):
        panel = read("Panel.qml")
        for prop in ("moduleName", "ipcTarget"):
            found = re.search(rf'{prop}:\s*"([^"]+)"', panel)
            self.assertIsNotNone(found, f"Panel.qml has no {prop}")
            self.assertEqual(
                found.group(1), self.plugin_id,
                f"Panel.qml {prop} does not match manifest.json",
            )

    def test_install_sh_does_not_hardcode_an_id(self):
        """It reads the manifest, so it cannot disagree with it."""
        script = read("install.sh")
        self.assertNotIn(f'PLUGIN_ID="{self.plugin_id}"', script)
        self.assertIn("manifest.json", script)

    def test_no_file_mentions_a_different_plugin_id(self):
        offenders = {}
        for root, _dirs, files in os.walk(ROOT):
            if ".git" in root:
                continue
            for name in files:
                if not name.endswith((".md", ".qml", ".js", ".json", ".sh", ".py")):
                    continue
                path = os.path.join(root, name)
                if os.path.samefile(path, __file__):
                    # This file quotes the old id while explaining the check.
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        text = fh.read()
                except (OSError, UnicodeDecodeError):
                    continue
                stale = {i for i in self.looks_like_a_plugin_id(text)
                         if i != self.plugin_id}
                if stale:
                    offenders[os.path.relpath(path, ROOT)] = sorted(stale)
        self.assertEqual(
            offenders, {},
            f"these still name an id that is not {self.plugin_id!r}",
        )


if __name__ == "__main__":
    unittest.main()
