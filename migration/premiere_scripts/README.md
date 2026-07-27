# Running the Premiere toolkit (`slyburn_premiere_toolkit.jsx`)

Premiere Pro has no built-in "run a script" menu, so use the free
**ExtendScript Debugger** extension for VS Code — one-time setup, then
running the toolkit is two clicks.

## One-time setup

1. Install [VS Code](https://code.visualstudio.com) and, inside it, the
   **"ExtendScript Debugger"** extension (publisher: Adobe).
2. Open this `premiere_scripts` folder in VS Code
   (File → Open Folder…).
3. The included `.vscode/launch.json` already targets Premiere. If VS Code
   asks to create one instead, choose *ExtendScript* and set
   `"script": "${workspaceFolder}/slyburn_premiere_toolkit.jsx"`.

## Each run

1. Open your project in Premiere and make the sequence you're working on
   the **active sequence** (click its timeline).
2. In VS Code: Run → Start Debugging (F5). If asked which host, pick
   **Adobe Premiere Pro**.
3. The toolkit dialog appears inside Premiere. Pick a tool, hit Run.

Reports land next to your `.prproj` in a `slyburn_reports/` folder.

## The tools, in the order you'd use them on a sequence

| # | Tool | What it does |
|---|---|---|
| 1 | **AUDIT** | Drops colored markers + writes a report. Crucially it classifies every multicam/nest: **Red** = will lose media if you flatten it (the "files just disappear" ones) or can't be inspected; **Blue** = healthy multicam, flatten via UI is safe. Also marks time remaps (orange), speed changes (yellow), disabled clips (purple). |
| 2 | **SNAPSHOT** | Dumps every clip (position, in/out, media path) to JSON. Run once labeled `before_flatten`, then flatten your healthy multicams in the UI (select them → right-click → Multi-Camera → Flatten), then run again as `after_flatten`. Diff on any machine: `python3 ../tools/compare_snapshots.py before.json after.json` — anything the flatten silently ate shows up as **GONE** or **NO MEDIA PATH**. |
| 3 | **MOTION** | Exports Position/Scale/Rotation/Opacity for every clip that isn't at defaults (XML loses these). After importing into Resolve, `apply_motion_sidecar.py` re-applies the static values automatically and lists the keyframed ones for manual rebuild. |
| 4 | **RECONSTRUCT** | For red-flagged multicams/nests that resolve to a single real source clip: does your manual drill-down automatically — computes the source in/out from the container offsets, cuts the true source clip onto the topmost video track at the exact same timeline position, and **verifies** placement/duration to the half-frame. Originals are left underneath so you can eyeball-toggle before deleting them. Add one empty video track on top first (Sequence → Add Tracks). |
| 5 | **CLEAN** | Deletes all disabled clips — after showing you the full list and writing a log of every removal. |

## Limitations (so nothing surprises you)

- **Flatten itself is not scriptable** — Adobe never exposed that command.
  The toolkit instead tells you which multicams are safe to flatten
  (do those in the UI in one select-all pass) and rebuilds the broken ones
  directly from source, which is what you were doing by hand.
- RECONSTRUCT handles **video**; multicam **audio** is flagged in the
  report but must be handled manually (or finish audio in Premiere).
- RECONSTRUCT skips containers that have a speed change/remap on the outer
  clip, or where it can't unambiguously identify a single inner source —
  those are listed in the report as manual fixes rather than guessed at.
- It changes the master-clip in/out points of source media it places
  (that's how trimmed insertion works via the API). Harmless, but noted.
- Work on your `_CONFORM` duplicate, never the master sequence. Save
  before running anything that modifies the timeline (4 and 5) — undo
  works, but belt and suspenders.
