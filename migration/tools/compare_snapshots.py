#!/usr/bin/env python3
"""
Diff two timeline snapshots taken with the SNAPSHOT tool in
slyburn_premiere_toolkit.jsx -- typically one taken BEFORE flattening
multicams in Premiere and one AFTER.

This is the "did anything silently disappear?" check: Premiere's flatten
on a badly-built multicam can leave nothing behind, and on a 45-minute
timeline you will not spot it by eye.

Usage:
    python3 compare_snapshots.py before_flatten.json after_flatten.json

Reports per track:
  - clips present before but GONE after (the flatten ate them)
  - clips that changed from a real media path to no path (went offline)
  - clips whose position or in/out changed
  - new clips (expected: the flattened results)

No third-party dependencies. Python 3.8+.
"""

import argparse
import json
import sys


def load(path):
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    fps = doc.get("fps", 24)
    tracks = {}
    for c in doc.get("clips", []):
        tracks.setdefault(c["track"], []).append(c)
    for clips in tracks.values():
        clips.sort(key=lambda c: c["startSeconds"])
    return doc, tracks, fps


def frames(seconds, fps):
    return round(seconds * fps)


def match(before, after, fps, tol_frames):
    used = set()
    pairs, gone = [], []
    for cb in before:
        best, best_score = None, None
        for j, ca in enumerate(after):
            if j in used:
                continue
            overlap = (min(cb["endSeconds"], ca["endSeconds"])
                       - max(cb["startSeconds"], ca["startSeconds"]))
            if overlap <= 0:
                continue
            score = overlap + (10**6 if ca["name"] == cb["name"] else 0)
            if best_score is None or score > best_score:
                best_score, best = score, j
        if best is None:
            gone.append(cb)
        else:
            used.add(best)
            pairs.append((cb, after[best]))
    new = [ca for j, ca in enumerate(after) if j not in used]
    return pairs, gone, new


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("before_json")
    ap.add_argument("after_json")
    ap.add_argument("--tolerance", type=int, default=0, metavar="FRAMES",
                    help="ignore position drift up to this many frames")
    args = ap.parse_args(argv)

    try:
        doc_b, tracks_b, fps = load(args.before_json)
        doc_a, tracks_a, _ = load(args.after_json)
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"Sequence: {doc_b.get('sequence')}  ({fps:.3f} fps)")
    print(f"Before:   {args.before_json}  label={doc_b.get('label')}  "
          f"{sum(len(v) for v in tracks_b.values())} clips")
    print(f"After:    {args.after_json}  label={doc_a.get('label')}  "
          f"{sum(len(v) for v in tracks_a.values())} clips")

    problems = 0
    labels = sorted(set(tracks_b) | set(tracks_a),
                    key=lambda t: (t[0], int(t[1:])))
    for label in labels:
        before = tracks_b.get(label, [])
        after = tracks_a.get(label, [])
        pairs, gone, new = match(before, after, fps, args.tolerance)
        issues = []
        for c in gone:
            issues.append(f"  GONE after flatten:  [{c['tc']}] {c['name']} "
                          f"({frames(c['endSeconds'] - c['startSeconds'], fps)} frames)"
                          + ("  <-- was a multicam/nest"
                             if c.get("isMulticam") or c.get("isNest") else ""))
        for cb, ca in pairs:
            if cb.get("mediaPath") and not ca.get("mediaPath"):
                issues.append(f"  WENT OFFLINE:        [{cb['tc']}] {cb['name']} "
                              f"(had {cb['mediaPath']})")
            ds = frames(ca["startSeconds"] - cb["startSeconds"], fps)
            de = frames(ca["endSeconds"] - cb["endSeconds"], fps)
            if abs(ds) > args.tolerance or abs(de) > args.tolerance:
                issues.append(f"  MOVED:               [{cb['tc']}] {cb['name']}: "
                              f"start {ds:+d}f, end {de:+d}f")
            di = frames(ca["inSeconds"] - cb["inSeconds"], fps)
            if abs(di) > args.tolerance and not (cb.get("isMulticam") or cb.get("isNest")):
                issues.append(f"  SOURCE IN shifted:   [{cb['tc']}] {cb['name']}: {di:+d}f")
        info = []
        for c in new:
            info.append(f"  new clip (expected from flatten): [{c['tc']}] {c['name']}"
                        + ("  !! NO MEDIA PATH" if not c.get("mediaPath") else ""))
            if not c.get("mediaPath"):
                problems += 1

        status = "OK" if not issues else f"{len(issues)} problem(s)"
        print(f"\n{label}: {len(before)} before, {len(after)} after -- {status}")
        for line in issues:
            print(line)
        for line in info:
            print(line)
        problems += len(issues)

    print("\n" + "=" * 60)
    if problems:
        print(f"TOTAL: {problems} problem(s). Check each GONE/OFFLINE item -- "
              "that is media the flatten dropped.")
        return 1
    print("TOTAL: nothing lost. Every clip accounted for.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
