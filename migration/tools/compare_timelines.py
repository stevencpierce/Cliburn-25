#!/usr/bin/env python3
"""
Compare a Premiere Pro timeline against the same timeline after import
into DaVinci Resolve, to catch dropped clips, shifted cuts, and
one-frame drift introduced by the XML round trip.

Workflow:
  1. Premiere:  File > Export > Final Cut Pro XML  ->  seq_premiere.xml
  2. Resolve:   import that XML, then right-click the timeline >
                Timelines > Export > FCP7 XML       ->  seq_resolve.xml
  3. Run:       python3 compare_timelines.py seq_premiere.xml seq_resolve.xml

Reports, per track:
  - clips present in Premiere but missing in Resolve
  - clips present in Resolve but not in Premiere (unexpected extras)
  - clips whose record in/out (position on timeline) shifted
  - clips whose source in/out (the frames used from the media) shifted

A tolerance (default 0 frames) can be set with --tolerance to ignore
sub-threshold drift once you've decided you can live with it.

No third-party dependencies. Python 3.8+.
"""

import argparse
import os
import sys
import xml.etree.ElementTree as ET

from preflight_check import frames_to_tc, int_text, rate_of


def load_sequence(path):
    tree = ET.parse(path)
    root = tree.getroot()
    if root.tag != "xmeml":
        raise ValueError(f"{path}: not an FCP7 XML (root <{root.tag}>)")
    seq = root.find(".//sequence")
    if seq is None:
        raise ValueError(f"{path}: no <sequence> found")
    return seq


def extract_clips(seq):
    """Return {track_label: [clip dicts sorted by start]} plus timeline meta."""
    timebase, ntsc = rate_of(seq)
    tc_elem = seq.find("timecode")
    drop = False
    offset = 0
    if tc_elem is not None:
        drop = (tc_elem.findtext("displayformat") or "NDF").strip().upper() == "DF"
        offset = int_text(tc_elem, "frame", 0) or 0

    tracks = {}
    media = seq.find("media")
    if media is None:
        return tracks, (timebase, ntsc, drop, offset)
    for kind, node in (("V", media.find("video")), ("A", media.find("audio"))):
        if node is None:
            continue
        for i, track in enumerate(node.findall("track"), 1):
            label = f"{kind}{i}"
            clips = []
            for ci in track.findall("clipitem"):
                start = int_text(ci, "start")
                end = int_text(ci, "end")
                if start is None or end is None or start < 0:
                    continue  # transition-attached partials
                clips.append({
                    "name": ci.findtext("name") or "(unnamed)",
                    "start": start,
                    "end": end,
                    "in": int_text(ci, "in", -1),
                    "out": int_text(ci, "out", -1),
                })
            clips.sort(key=lambda c: c["start"])
            tracks[label] = clips
    return tracks, (timebase, ntsc, drop, offset)


def match_clips(a_clips, b_clips):
    """Greedy match by timeline overlap + name. Returns (pairs, only_a, only_b)."""
    used_b = set()
    pairs = []
    only_a = []
    for ca in a_clips:
        best = None
        best_overlap = 0
        for j, cb in enumerate(b_clips):
            if j in used_b:
                continue
            overlap = min(ca["end"], cb["end"]) - max(ca["start"], cb["start"])
            if overlap <= 0:
                continue
            # Prefer same-name matches, then largest overlap
            score = overlap + (10**9 if cb["name"] == ca["name"] else 0)
            if score > best_overlap:
                best_overlap = score
                best = j
        if best is None:
            only_a.append(ca)
        else:
            used_b.add(best)
            pairs.append((ca, b_clips[best]))
    only_b = [cb for j, cb in enumerate(b_clips) if j not in used_b]
    return pairs, only_a, only_b


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("premiere_xml", help="XML exported from Premiere (reference)")
    ap.add_argument("resolve_xml", help="XML exported from Resolve after import")
    ap.add_argument("--tolerance", type=int, default=0, metavar="FRAMES",
                    help="ignore position/source drift up to this many frames")
    ap.add_argument("--ignore-audio", action="store_true",
                    help="compare video tracks only")
    args = ap.parse_args(argv)

    try:
        seq_a = load_sequence(args.premiere_xml)
        seq_b = load_sequence(args.resolve_xml)
    except (ET.ParseError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    tracks_a, (tb_a, ntsc_a, drop_a, off_a) = extract_clips(seq_a)
    tracks_b, (tb_b, ntsc_b, drop_b, off_b) = extract_clips(seq_b)

    def tc(frames):
        return frames_to_tc(frames + off_a, tb_a, drop_a)

    problems = 0
    print(f"Premiere: {os.path.basename(args.premiere_xml)}  "
          f"({seq_a.findtext('name')}, {tb_a}{'*1000/1001' if ntsc_a else ''} fps)")
    print(f"Resolve:  {os.path.basename(args.resolve_xml)}  "
          f"({seq_b.findtext('name')}, {tb_b}{'*1000/1001' if ntsc_b else ''} fps)")

    if (tb_a, ntsc_a) != (tb_b, ntsc_b):
        print(f"\n!! TIMEBASE MISMATCH: Premiere {tb_a} (ntsc={ntsc_a}) vs "
              f"Resolve {tb_b} (ntsc={ntsc_b}). Fix the Resolve timeline frame "
              "rate before comparing anything else.")
        return 1
    if off_a != off_b:
        print(f"\n!! START TC MISMATCH: Premiere starts at frame {off_a} "
              f"({frames_to_tc(off_a, tb_a, drop_a)}), Resolve at {off_b} "
              f"({frames_to_tc(off_b, tb_b, drop_b)}). Set the Resolve timeline "
              "start timecode to match, or every position will read as shifted.")
        problems += 1

    all_labels = sorted(set(tracks_a) | set(tracks_b),
                        key=lambda t: (t[0], int(t[1:])))
    for label in all_labels:
        if args.ignore_audio and label.startswith("A"):
            continue
        a_clips = tracks_a.get(label, [])
        b_clips = tracks_b.get(label, [])
        pairs, only_a, only_b = match_clips(a_clips, b_clips)

        issues = []
        for ca in only_a:
            issues.append(f"  MISSING in Resolve: [{tc(ca['start'])}] {ca['name']} "
                          f"({ca['end'] - ca['start']} frames)")
        for cb in only_b:
            issues.append(f"  EXTRA in Resolve:   [{tc(cb['start'])}] {cb['name']} "
                          f"({cb['end'] - cb['start']} frames)")
        for ca, cb in pairs:
            ds, de = cb["start"] - ca["start"], cb["end"] - ca["end"]
            if abs(ds) > args.tolerance or abs(de) > args.tolerance:
                issues.append(
                    f"  POSITION shift:     [{tc(ca['start'])}] {ca['name']}: "
                    f"start {ds:+d}f, end {de:+d}f")
            if ca["in"] >= 0 and cb["in"] >= 0:
                di, do = cb["in"] - ca["in"], cb["out"] - ca["out"]
                if abs(di) > args.tolerance or abs(do) > args.tolerance:
                    issues.append(
                        f"  SOURCE shift:       [{tc(ca['start'])}] {ca['name']}: "
                        f"in {di:+d}f, out {do:+d}f (different frames of media!)")

        status = "OK" if not issues else f"{len(issues)} issue(s)"
        print(f"\n{label}: {len(a_clips)} clips in Premiere, "
              f"{len(b_clips)} in Resolve -- {status}")
        for line in issues:
            print(line)
        problems += len(issues)

    print(f"\n{'=' * 60}")
    if problems:
        print(f"TOTAL: {problems} discrepancies. Fix, re-export from Resolve, re-run.")
        return 1
    print("TOTAL: timelines match within tolerance. Conform verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
