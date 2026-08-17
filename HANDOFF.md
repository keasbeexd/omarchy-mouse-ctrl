# Handoff

This project was built in a Cowork session that had no access to the mouse and
no write access to GitHub. Everything below is the state at handoff.

## Done

- Protocol decoded from the vendor Windows driver by static analysis — see
  `CLAUDE.md` for the wire format and `docs/PROTOCOL-DISCOVERY.md` for method.
- `hskctl`: dependency-free HID CLI, talks to `/dev/hidraw*` via kernel ioctls.
- Omarchy Quattro plugin at the repo root, CLI bundled, one-line install.
- 30 Python + 33 JS tests, all passing, pinning the decoded protocol.
- Discovery tooling kept in `tools/` in case another model needs profiling.

## Never run against real hardware

Nothing in this repo has touched an actual HSK Pro 4K. The transport and
command table came out of the vendor binary and are solid; the value maps are
flagged `_needsVerification` and must be confirmed before they are trusted.

**First thing to do on a machine with the mouse attached:**

```bash
hskctl probe      # should pick the interface-2 node, VID 33e4
hskctl status     # compare every line against the Windows app
hskctl fields     # lists what is writable and what is unverified
```

If `status` disagrees with the vendor app anywhere, fix the profile before
writing anything.

## Picking this up in Claude Code

`CLAUDE.md` is the briefing — Claude Code reads it automatically. The short
version of what matters:

- The profile is data. Device knowledge does not go into Python.
- `_needsVerification` fields do not get "corrected" by reasoning.
- Factory reset (opcode `09`) stays unbound. A test enforces it.
- A cloud session has no mouse; `probe` finding nothing is correct behaviour.

Good first tasks that need no hardware:

- Run `tools/analyze-driver.py` against the 2023 build
  (`HSK_Pro_4K_FWSW20230322.rar`) and diff the command table against the 2024
  one, as a cross-check.
- Decode the seven-stage DPI write packet from `hts_set_get_dpis_colors` well
  enough to enable DPI writes behind a flag.
- `hskctl status --json` currently issues one command per distinct field
  source; confirm that is one exchange per command and not per field.

Tasks that need the mouse are listed at the bottom of `CLAUDE.md`.
