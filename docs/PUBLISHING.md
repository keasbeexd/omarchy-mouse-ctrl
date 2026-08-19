# Listing this on omarchyplugins.com

The marketplace is a community registry: you open an issue on
[HANCORE-linux/omarchy-plugin-marketplace][repo], automated checks run against
the current commit, and a maintainer applies `approved-for-listing`. Listing is
**not** a security review — plugins run unsandboxed, and that is on us.

[repo]: https://github.com/HANCORE-linux/omarchy-plugin-marketplace

## Before submitting

**1. `preview.png` is a real screenshot** of the panel, not a rendering. It was
a drawn mockup for a while; if you ever need to retake it:

```bash
hyprshot -m region -o . -f preview.png     # or grim/slurp
```

Frame enough of the bar to show the widget in context, and the whole panel
including the sensor toggles at the bottom. Wider is better — listing cards are
landscape, so a tall narrow crop gets letterboxed.

**2. Check that the remote matches what you tested.** This is what got the
first submission rejected: the pushed tree had a truncated `install.sh` and was
missing a file it referenced, neither of which was true locally. Reviewers read
the remote, not your working copy.

```bash
git fetch origin
git diff --stat origin/main HEAD     # must be empty
git status --short                   # must be empty
```

**3. Validate locally.** The marketplace runs its own check; this catches a bad
manifest before a maintainer sees it.

```bash
omarchy plugin validate
python3 -m unittest discover -s tests   # includes the packaging checks
node tests/test_model.js
```

The packaging tests exist because of that rejection — they assert that
`install.sh` parses and handles every flag it documents, that nothing
references a file the tree does not ship, and that the README's links and
commands are real.

**4. Check the repository is public**, and that `manifest.json`, `README.md`,
`LICENSE` and `preview.png` are all at the root.

## The submission

Open <https://github.com/HANCORE-linux/omarchy-plugin-marketplace/issues/new?template=submit-plugin.yml>
and fill it in, or use the CLI:

```bash
gh issue create \
  --repo HANCORE-linux/omarchy-plugin-marketplace \
  --title "[Plugin]: HSK Mouse" \
  --body-file docs/submission.md
```

| Field | Value |
|---|---|
| Repository URL | `https://github.com/keasbeexd/omarchy-hsk` |
| Category | **Hardware** |
| Tags | **Bar**, **Quickshell**, **System** |
| Maintainer notes | see `docs/submission.md` |

The form allows at most three tags and rejects submissions with more.

## After it is listed

Version lives in `manifest.json` and is what the marketplace displays. Bump it
when you ship, and tag the release so people can tell what they are installing:

```bash
git tag -a v1.0.0 -m "First public release" && git push --tags
```
