# Getting this onto GitHub

The repo is already initialised here with full history — it just needs
publishing. Pick whichever line applies.

## If `gh` is authenticated on your machine

```bash
cd omarchy-hsk
gh repo create keasbeexd/omarchy-hsk --public --source=. --remote=origin --push
```

Creates the repo and pushes `main` in one go.

## Plain git

Create an empty repo at <https://github.com/new> named `omarchy-hsk` — no
README, no .gitignore, no licence, since this already has all three — then:

```bash
cd omarchy-hsk
git remote add origin https://github.com/keasbeexd/omarchy-hsk.git
git push -u origin main
```

## Check the commit author first

Commits are currently attributed to:

```
Marc <keasbee@users.noreply.github.com>
```

GitHub noreply addresses are usually `<id>+<username>@users.noreply.github.com`
— yours would be `234014364+keasbeexd@users.noreply.github.com`. To use that
instead, before pushing:

```bash
git config user.email "234014364+keasbeexd@users.noreply.github.com"
git config user.name "Marc"
git rebase --root --exec 'git commit --amend --reset-author --no-edit'
```

Without this the commits still land, they just may not link to your profile.

## Installing from the repo afterwards

`manifest.json` is at the repo root, so once it is public:

```bash
omarchy plugin add https://github.com/keasbeexd/omarchy-hsk.git
omarchy plugin enable io.github.keasbeexd.hsk
```

The CLI ships inside the plugin, so there is nothing else to install. The
optional udev rule lives at `./install.sh --udev` inside the cloned plugin
directory.
