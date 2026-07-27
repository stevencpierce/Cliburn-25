# Per-sequence conform checklist

Copy this block once per sequence and check items off as you go.
A sequence is DONE only when every box is checked or has a written waiver.

---

## Sequence: ____________  (editor: ____ , date started: ____ )

### Premiere prep
- [ ] Duplicated sequence as `<SEQ>_CONFORM` (never prep the master)
- [ ] All multicam clips flattened
- [ ] All nested sequences un-nested or rendered & replaced
- [ ] Disabled clips deleted
- [ ] MOGRTs/titles handled (rebuild list written, or baked to ProRes 4444+alpha)
- [ ] Warp Stabilizer shots handled (re-stabilize list, or rendered & replaced)
- [ ] Reference QuickTime exported (with TC burn-in)
- [ ] FCP7 XML exported → `exports/SLY_<seq>_premiere.xml`

### Pre-flight
- [ ] `preflight_check.py` run; report saved to `reports/`
- [ ] All **WILL NOT TRANSLATE** items resolved in Premiere or accepted for rebuild
- [ ] Punch list (CSV) saved for post-import checks

### Resolve import
- [ ] Project frame rate/resolution verified BEFORE import (23.976 vs 24!)
- [ ] Timeline imported (UI or `resolve_conform.py import`)
- [ ] Timeline start TC matches Premiere (e.g. 01:00:00:00)
- [ ] `resolve_conform.py audit` → zero offline clips (relinked as needed)

### Verification
- [ ] Timeline exported from Resolve → `roundtrip/`
- [ ] `compare_timelines.py` clean (or every diff has a written reason below)
- [ ] Speed ramps rebuilt (from preflight list)
- [ ] Titles/graphics rebuilt or baked versions cut in
- [ ] Transitions checked at every flagged TC
- [ ] Mixed-frame-rate clips: Clip Attributes verified against Premiere interpret
- [ ] Audio: channel mapping spot-checked; clip volume keyframes spot-checked
- [ ] Full playback against reference QuickTime (2× with punch list)

### Sign-off
- [ ] Compare script final run attached/committed
- [ ] TRACKER.md updated
- [ ] Signed off by: ____________  date: ____

**Waivers / accepted diffs:**

- _(none)_

---
