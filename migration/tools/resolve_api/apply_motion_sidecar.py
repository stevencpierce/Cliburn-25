#!/usr/bin/env python3
"""
Re-apply Premiere Motion data (Position / Scale / Rotation / Opacity) to a
DaVinci Resolve timeline, from the sidecar JSON exported by the MOTION tool
in slyburn_premiere_toolkit.jsx.

Why: the FCP7 XML round trip loses (or mangles) Premiere's Motion values,
so repos/scale-ups on archival footage etc. arrive flat. This script sets
Resolve's Pan/Tilt/Zoom/Rotation/Opacity on the matching timeline clips.

Static values are applied automatically. KEYFRAMED values cannot be set
through Resolve's API -- those clips are listed at the end for manual
rebuild (the sidecar tells you it was animated; eyeball the Premiere ref
for the move).

Usage (Resolve open, project loaded, same env vars as resolve_conform.py):
    python3 apply_motion_sidecar.py SLY_EP1_motion.json --timeline "SLY_EP1"
    python3 apply_motion_sidecar.py SLY_EP1_motion.json --timeline "SLY_EP1" --dry-run

Coordinate conversion (verify on the first clip and use --flip-tilt /
--flip-pan if your footage moves the wrong way):
    Premiere Position is normalized, (0.5, 0.5) = frame center, y down.
    Resolve Pan/Tilt are pixel offsets from center; this script uses
    Pan = (x - 0.5) * width,  Tilt = (0.5 - y) * height.
    Zoom = scale / 100.
"""

import argparse
import json
import sys

from resolve_conform import connect


def find_timeline(project, name):
    for i in range(1, project.GetTimelineCount() + 1):
        tl = project.GetTimelineByIndex(i)
        if tl and tl.GetName() == name:
            return tl
    return None


def build_index(tl):
    """{(name, start_frame): timeline_item} across all video tracks."""
    index = []
    for track in range(1, tl.GetTrackCount("video") + 1):
        for item in tl.GetItemListInTrack("video", track) or []:
            index.append((item.GetName(), item.GetStart(), track, item))
    return index


def match_item(index, name, want_frame, tol=2):
    best = None
    for item_name, start, track, item in index:
        if abs(start - want_frame) > tol:
            continue
        score = (item_name == name, -abs(start - want_frame))
        if item_name == name or name in item_name or item_name in name:
            if best is None or score > best[0]:
                best = (score, item, track)
    return (best[1], best[2]) if best else (None, None)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("sidecar", help="motion JSON from the Premiere MOTION tool")
    ap.add_argument("--timeline", required=True, help="Resolve timeline name")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be set without changing anything")
    ap.add_argument("--flip-tilt", action="store_true",
                    help="negate Tilt if vertical repos move the wrong way")
    ap.add_argument("--flip-pan", action="store_true",
                    help="negate Pan if horizontal repos move the wrong way")
    ap.add_argument("--tolerance", type=int, default=2, metavar="FRAMES",
                    help="clip-matching tolerance (default 2 frames)")
    args = ap.parse_args(argv)

    with open(args.sidecar, encoding="utf-8") as fh:
        doc = json.load(fh)
    entries = doc.get("entries", [])
    if not entries:
        print("Sidecar has no entries -- nothing to do.")
        return 0

    _, project = connect()
    tl = find_timeline(project, args.timeline)
    if tl is None:
        sys.exit(f"Timeline '{args.timeline}' not found in project "
                 f"'{project.GetName()}'.")
    fps = float(tl.GetSetting("timelineFrameRate"))
    tl_start = tl.GetStartFrame()
    index = build_index(tl)
    print(f"Timeline '{args.timeline}': {len(index)} video clips, "
          f"{fps} fps, start frame {tl_start}")

    applied, keyframed, unmatched = 0, [], []
    for e in entries:
        want_frame = round(e["startSeconds"] * fps) + tl_start
        item, track = match_item(index, e["name"], want_frame, args.tolerance)
        label = f"[{e['tc']}] {e['name']}"
        if item is None:
            unmatched.append(label)
            continue

        if any(e.get(k) for k in ("positionKeyframed", "scaleKeyframed",
                                  "rotationKeyframed", "opacityKeyframed")):
            keyframed.append(label + "  (animated in Premiere -- rebuild by hand)")
            continue

        w = e.get("frameWidth") or doc.get("frameWidth") or 1920
        h = e.get("frameHeight") or doc.get("frameHeight") or 1080
        props = {}
        pos = e.get("positionNorm")
        if isinstance(pos, (list, tuple)) and len(pos) >= 2:
            pan = (pos[0] - 0.5) * w
            tilt = (0.5 - pos[1]) * h
            if args.flip_pan:
                pan = -pan
            if args.flip_tilt:
                tilt = -tilt
            if abs(pan) > 0.01:
                props["Pan"] = pan
            if abs(tilt) > 0.01:
                props["Tilt"] = tilt
        scale = e.get("scalePercent")
        if scale is not None and abs(scale - 100) > 0.01:
            props["ZoomX"] = scale / 100.0
            props["ZoomY"] = scale / 100.0
        sw = e.get("scaleWidthPercent")
        if sw is not None and abs(sw - 100) > 0.01:
            props["ZoomX"] = sw / 100.0  # non-uniform scale
        rot = e.get("rotationDegrees")
        if rot is not None and abs(rot) > 0.01:
            props["RotationAngle"] = rot
        opac = e.get("opacityPercent")
        if opac is not None and abs(opac - 100) > 0.01:
            props["Opacity"] = opac

        if not props:
            continue
        desc = ", ".join(f"{k}={v:.2f}" for k, v in props.items())
        if args.dry_run:
            print(f"  DRY: {label} (V{track}) -> {desc}")
            applied += 1
            continue
        ok = all(item.SetProperty(k, v) for k, v in props.items())
        print(f"  {'OK ' if ok else 'FAIL'}: {label} (V{track}) -> {desc}")
        if ok:
            applied += 1

    print(f"\nApplied {applied}/{len(entries)} entries"
          f"{' (dry run)' if args.dry_run else ''}.")
    if keyframed:
        print(f"\n{len(keyframed)} clip(s) had KEYFRAMED motion -- rebuild manually:")
        for line in keyframed:
            print("  " + line)
    if unmatched:
        print(f"\n{len(unmatched)} entr(ies) had no matching clip in Resolve:")
        for line in unmatched:
            print("  " + line)
    return 1 if unmatched else 0


if __name__ == "__main__":
    sys.exit(main())
