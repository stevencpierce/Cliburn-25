# Slyburn: Premiere Pro → DaVinci Resolve Migration Kit

Toolkit and playbook for moving the Slyburn documentary sequences (~45 min each)
from Premiere Pro into DaVinci Resolve without losing cuts, sync, or your mind.

The core idea: **never trust the import — verify it mechanically.** Every
sequence goes through the same loop:

```
Premiere prep → export XML → PREFLIGHT script → import to Resolve
→ export XML back out of Resolve → COMPARE script → fix punch list → sign off
```

## What's in here

| Path | What it does |
|---|---|
| `tools/preflight_check.py` | Scans a Premiere FCP7 XML export and flags everything that won't translate (speed ramps, nests, titles/MOGRTs, freeze frames, odd transitions, mixed frame rates, offline media…) with timecodes. |
| `tools/compare_timelines.py` | Diffs the Premiere XML against the XML re-exported from Resolve after import. Catches dropped clips, shifted cuts, and one-frame drift. |
| `tools/resolve_api/resolve_conform.py` | Runs against a live Resolve session: batch-imports XMLs, exports timelines back to XML, and audits timelines for offline/unlinked clips. |
| `CHECKLIST.md` | The per-sequence conform checklist (copy one block per sequence). |
| `TRACKER.md` | One-page status board for all sequences. |

All scripts are Python 3.8+ standard library only — nothing to install.
`preflight_check.py` and `compare_timelines.py` run anywhere;
`resolve_conform.py` must run on the machine where Resolve is open.

---

## Step 0 — One-time setup

**In Resolve** (once per workstation):

- Preferences → System → General → *External scripting using:* **Local**
  (needed for `resolve_conform.py`).
- Project Settings → Master Settings: set **timeline resolution and frame rate
  to match the Premiere sequences exactly** (e.g. 23.976, not 24) *before*
  importing anything. Resolve locks timeline frame rate after creation.
- Project Settings → General Options: enable **"Use local version"** defaults as
  you prefer, and note the **Standard still duration** — it can override still
  lengths on import.

**Terminal env vars** for the Resolve API (macOS):

```bash
export RESOLVE_SCRIPT_API="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
export RESOLVE_SCRIPT_LIB="/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
export PYTHONPATH="$PYTHONPATH:$RESOLVE_SCRIPT_API/Modules/"
```

Windows:

```
set RESOLVE_SCRIPT_API=%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting
set RESOLVE_SCRIPT_LIB=C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll
set PYTHONPATH=%PYTHONPATH%;%RESOLVE_SCRIPT_API%\Modules
```

---

## Step 1 — Prep the sequence in Premiere (manual, ~10 min)

Work on a **duplicate** of the sequence, named `<SEQ>_CONFORM`:

1. **Flatten all multicam clips** (right-click → Multi-Camera → Flatten).
2. **Un-nest or bake nested sequences.** Either copy the nest's contents up into
   the main timeline, or render & replace the nest with a ProRes file.
3. **Render & replace anything the preflight will flag as unbuildable** if you
   want to keep the look rather than rebuild it: MOGRTs/titles you're keeping
   as-is → export as ProRes 4444 + alpha; heavy Warp Stabilizer shots you don't
   want to re-stabilize → render & replace.
4. **Delete disabled clips** and anything on muted tracks you don't need.
5. **Remove Lumetri** only if it clutters — it won't travel either way.
6. Leave audio simple: clip volume keyframes usually travel; **track mixer
   settings, submixes, and audio track effects do not.** Note anything you need
   to recreate (or plan to finish audio in Premiere/Pro Tools and marry later).
7. Reasonable track hygiene: no clips stranded far above V6 by accident, etc.

Then: **File → Export → Final Cut Pro XML…** → save as `SLY_<seq>_premiere.xml`.

> Also export a **reference movie** of the sequence (with TC burn-in if you
> like) — you'll eyeball the Resolve import against it, and it's the ultimate
> tie-breaker when the compare script reports a drift.

## Step 2 — Pre-flight the XML

```bash
python3 migration/tools/preflight_check.py SLY_ep1_premiere.xml \
    --md reports/SLY_ep1_preflight.md --csv reports/SLY_ep1_flags.csv
```

- **WILL NOT TRANSLATE** items = go back to Premiere and bake/flatten/replace
  them now (cheaper than fixing in Resolve), or accept the rebuild.
- **CHECK AFTER IMPORT** items = your punch list for Step 4.
- Exit code is 1 when blocking items exist, so you can loop it over a folder of
  XMLs and see which sequences still need Premiere prep.

## Step 3 — Import into Resolve

Either through the UI (File → Import → Timeline → keep an eye on the import
options) or in bulk:

```bash
python3 migration/tools/resolve_api/resolve_conform.py import exports/*.xml \
    --media-root "/Volumes/SLYBURN/MEDIA"
python3 migration/tools/resolve_api/resolve_conform.py audit
```

Import-dialog settings that matter (UI import):

- ✅ Automatically import source clips into media pool
- ✅ Use sizing information (keeps position/scale moves)
- ❌ "Ignore file extensions when matching" — leave off unless relinking to
  transcodes with a different codec/extension.
- Set timeline start TC to match the Premiere sequence (usually 01:00:00:00).

Relink anything the `audit` reports as offline before going further
(Media Pool → right-click → Relink Selected Clips).

## Step 4 — Verify mechanically, then by eye

Export the imported timeline back out of Resolve and diff it:

```bash
python3 migration/tools/resolve_api/resolve_conform.py export --all --out roundtrip/
python3 migration/tools/compare_timelines.py \
    exports/SLY_ep1_premiere.xml roundtrip/SLY_EP1_resolve.xml
```

- Fix every **MISSING / EXTRA / SOURCE shift** — those are real conform errors.
- **POSITION shifts** of ±1 frame cluster around transitions and speed changes;
  check them against the reference movie.
- Then play the timeline against the reference export at 2× with the punch
  list (preflight CSV) beside you.

## Step 5 — Sign off

Work through the block for this sequence in `CHECKLIST.md`, update `TRACKER.md`,
commit both. A sequence isn't "migrated" until the compare script returns clean
(or every remaining diff has a written reason) and picture-lock playback matches
the reference.

---

## Known Premiere → Resolve gotchas (the bug list)

| Area | What happens | What to do |
|---|---|---|
| **Speed ramps / time remap** | Do not translate; clip imports at 100% or wrong length | Rebuild with Retime Curve in Resolve (preflight flags each one) |
| **Constant speed changes** | Usually survive, occasionally ±1 frame | Compare script catches drift |
| **Nested sequences** | Import flat, offline, or as a single mystery clip | Flatten/bake before export |
| **Multicam** | Same as nests | Flatten before export |
| **Transitions** | Anything fancy becomes a plain cross dissolve or is dropped | Recreate; Resolve's Smooth Cut ≈ Morph Cut |
| **Titles / Essential Graphics / MOGRTs** | Offline placeholder or nothing | Rebuild as Text+ or bake to ProRes 4444 + alpha |
| **Lumetri color** | Stripped | Regrade in Resolve (the point of the move) |
| **Warp Stabilizer / Morph Cut / 3rd-party fx** | Stripped | Resolve stabilizer / Smooth Cut / rebuild |
| **Freeze frames** | Often offline or wrong frame | Rebuild via Retime Controls → Freeze Frame |
| **Merged clips** | Frequently offline or relink to wrong media | Avoid; re-sync in Resolve or render & replace |
| **Stills / PSDs** | Duration overrides, PSDs flatten | Check still duration setting; export PSD layers as PNG |
| **Audio: track effects, submixes, mixer automation** | Stripped (clip keyframes mostly survive) | Note settings; recreate in Fairlight, or finish audio elsewhere |
| **Stereo/dual-mono mapping** | Resolve may interpret channels differently | Check Clip Attributes → Audio on flagged clips |
| **Mixed frame rates** | "Interpret footage" settings don't travel | Set Clip Attributes in Resolve to match (preflight flags each mismatched source) |
| **Timeline start TC** | Occasionally lands at 00:00:00:00 | Set to match before comparing |
| **23.976 vs 24** | Project set to the wrong one = everything drifts | Match project settings before import; compare script hard-stops on mismatch |

## Suggested working structure on the edit drive

```
SLYBURN_CONFORM/
├── exports/          # XMLs out of Premiere        (SLY_ep1_premiere.xml)
├── reports/          # preflight .md/.csv reports
├── roundtrip/        # XMLs back out of Resolve    (SLY_EP1_resolve.xml)
└── refs/             # reference QuickTimes with TC burn-in
```
