# profiles/

This plugin ships with no device profiles.

A profile is something the **guided setup** writes for *your* mouse into
`~/.config/hskctl/profiles/` — byte layouts the plugin has no first-hand
knowledge of your hardware, so it carries no files for mice its users never
own. `profiles/` here existed historically and is kept as a dev inbox: you can
drop a working profile in it for local testing, and `hskctl` finds it
automatically (first in the search path, ahead of the user directory).

Protocol knowledge recovered from real hardware lives in
`docs/contributor/families/*.json` and in the per-model notes under
`docs/profiles/` — that is the seed data the guided setup diff-rounds are
built on, not something the plugin presents as its own.
