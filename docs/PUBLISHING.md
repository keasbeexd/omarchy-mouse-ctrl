# Listing this on omarchyplugins.com

The marketplace is a community registry: you open an issue on
[HANCORE-linux/omarchy-plugin-marketplace][repo], automated checks run against
the current commit, and a maintainer applies `approved-for-listing`. Listing is
**not** a security review — plugins run unsandboxed, and that is on us.

[repo]: https://github.com/HANCORE-linux/omarchy-plugin-marketplace

## Before submitting

**1. Replace `preview.png` with a real screenshot.**

The one in the repository is a *mockup*, drawn by `tools/make-preview.py` from
`Panel.qml`'s layout so the repo was complete before anyone could screenshot it.
It is accurate, but it is a drawing, and the listing deserves the real thing —
your bar, your theme, your DPI stages.

```bash
hyprshot -m region -o . -f preview.png     # or grim/slurp, or your usual tool
```

Frame it like the mockup: enough of the bar to show the widget in context, the
panel open beside it, 1280×800 or wider. Then delete `tools/make-preview.py` if
you would rather not carry it.

**2. Validate locally.** The marketplace runs its own check, but this catches a
bad manifest before a maintainer sees it.

```bash
omarchy plugin validate
python3 -m unittest discover -s tests
node tests/test_model.js
```

**3. Check the repository is public**, and that `manifest.json`, `README.md`,
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
