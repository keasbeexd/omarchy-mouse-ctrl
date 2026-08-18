#!/usr/bin/env bash
# Apply the newest git bundle from your downloads folder, push it, and reload
# the Omarchy shell.
#
#   ./tools/apply-update.sh              # newest *.bundle in ~/Downloads
#   ./tools/apply-update.sh path/to.bundle
#   ./tools/apply-update.sh --no-push    # apply locally, push later
#   ./tools/apply-update.sh --no-reload  # skip omarchy-restart-shell
#
# Nothing here rewrites history: it fast-forwards or it stops. If the merge
# would not be a fast-forward, that means there are local commits the bundle
# does not contain, and resolving that is a decision for you rather than a
# script.

set -euo pipefail

SEARCH_DIR="${HSK_BUNDLE_DIR:-$HOME/Downloads}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE=""
DO_PUSH=1
DO_RELOAD=1

info() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m warn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

for arg in "$@"; do
  case "$arg" in
    --no-push)   DO_PUSH=0 ;;
    --no-reload) DO_RELOAD=0 ;;
    -h|--help)   awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
    -*)          die "unknown option: $arg" ;;
    *)           BUNDLE="$arg" ;;
  esac
done

# --- find the bundle --------------------------------------------------------
# The download often arrives renamed (hyphens stripped, a suffix added), so
# pick the newest .bundle rather than expecting an exact filename.
if [[ -z "$BUNDLE" ]]; then
  BUNDLE="$(ls -t "$SEARCH_DIR"/*.bundle 2>/dev/null | head -1 || true)"
  [[ -n "$BUNDLE" ]] || die "no .bundle found in $SEARCH_DIR (pass a path, or set HSK_BUNDLE_DIR)"
fi
[[ -f "$BUNDLE" ]] || die "$BUNDLE does not exist"

cd "$REPO_DIR"
git rev-parse --git-dir >/dev/null 2>&1 || die "$REPO_DIR is not a git repository"

git bundle verify "$BUNDLE" >/dev/null 2>&1 \
  || die "$BUNDLE is not a valid git bundle (a truncated download looks like this)"

info "Using $(basename "$BUNDLE")  ($(du -h "$BUNDLE" | cut -f1))"

# --- refuse to clobber uncommitted work -------------------------------------
if [[ -n "$(git status --porcelain)" ]]; then
  git status --short
  die "you have uncommitted changes. Commit or stash them first."
fi

BEFORE="$(git rev-parse HEAD)"

# --- apply ------------------------------------------------------------------
git fetch --quiet "$BUNDLE" main:refs/remotes/update/main --force
if ! git merge --ff-only refs/remotes/update/main >/dev/null 2>&1; then
  if git merge-base --is-ancestor refs/remotes/update/main HEAD; then
    info "Already contains this bundle."
  else
    die "cannot fast-forward -- you have local commits the bundle does not. Run:
       git log --oneline refs/remotes/update/main..HEAD"
  fi
fi

AFTER="$(git rev-parse HEAD)"
if [[ "$BEFORE" == "$AFTER" ]]; then
  info "Nothing new to apply."
else
  info "Applied:"
  git --no-pager log --oneline "$BEFORE..$AFTER" | sed 's/^/     /'
fi

# --- push -------------------------------------------------------------------
if (( DO_PUSH )); then
  if git remote get-url origin >/dev/null 2>&1; then
    if [[ -n "$(git log --oneline @{u}..HEAD 2>/dev/null)" ]]; then
      info "Pushing"
      git push
    else
      info "Already pushed."
    fi
  else
    warn "no origin remote, skipping push"
  fi
fi

# --- reload -----------------------------------------------------------------
# The plugin directory is usually a symlink to this checkout (install.sh --dev),
# in which case the files are already live and only the shell needs restarting.
# If it is a separate clone, it has to pull for itself.
PLUGIN_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/io.github.keasbeexd.hsk"
if [[ -d "$PLUGIN_DIR" && ! -L "$PLUGIN_DIR" ]]; then
  if [[ "$(cd "$PLUGIN_DIR" && git rev-parse HEAD 2>/dev/null)" != "$AFTER" ]]; then
    info "Plugin directory is a separate clone -- pulling it too"
    git -C "$PLUGIN_DIR" pull --ff-only --quiet || warn "could not fast-forward $PLUGIN_DIR"
  fi
fi

if (( DO_RELOAD )); then
  if command -v omarchy-restart-shell >/dev/null 2>&1; then
    info "Restarting the shell"
    # A rescan alone does not reliably pick up QML changes; a restart does.
    omarchy-restart-shell
  else
    warn "omarchy-restart-shell not found -- reload the shell yourself"
  fi
fi

info "Done."
