#!/usr/bin/env python3
"""
Pre-flight checker for Premiere Pro -> DaVinci Resolve sequence migration.

Parses a Final Cut Pro 7 XML (xmeml) exported from Premiere Pro
(File > Export > Final Cut Pro XML...) and flags everything known to
translate badly (or not at all) when imported into DaVinci Resolve,
with timeline timecodes so you can walk the sequence with a punch list.

Usage:
    python3 preflight_check.py sequence.xml
    python3 preflight_check.py sequence.xml --md report.md --csv flags.csv
    python3 preflight_check.py exports/*.xml --md-dir reports/

No third-party dependencies. Python 3.8+.
"""

import argparse
import csv
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Knowledge base: how things translate from Premiere XML into Resolve
# ---------------------------------------------------------------------------

# Effects that will NOT come across and must be rebuilt in Resolve.
EFFECTS_NOT_TRANSLATED = {
    "warp stabilizer": "Redo with Resolve stabilizer (Inspector > Stabilization, or Fusion).",
    "morph cut": "No equivalent auto-translate; use Smooth Cut transition in Resolve.",
    "lumetri color": "Grades do not travel via XML; regrade in Resolve (that's the point of the move).",
    "lumetri": "Grades do not travel via XML; regrade in Resolve.",
    "time remap": "Speed ramps must be rebuilt with Resolve's Retime Curve.",
    "timeremap": "Speed ramps must be rebuilt with Resolve's Retime Curve.",
}

# Effect names that generally DO translate (basic geometry / opacity).
EFFECTS_USUALLY_OK = {
    "basic motion", "basicmotion", "motion", "opacity", "crop", "audio levels",
    "volume", "levels", "pan", "stereo pan", "audio pan",
}

# Transitions other than a plain cross dissolve typically import as a plain
# cross dissolve (or get dropped) in Resolve.
TRANSITION_OK = {"cross dissolve", "crossdissolve", "dip to black", "dip to white"}

STILL_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".psd", ".ai", ".bmp", ".gif",
    ".exr", ".dpx", ".heic", ".webp",
}

GRAPHIC_HINTS = ("mogrt", "graphic", "title", "lower third", "lowerthird", "l3rd")

SEV_BLOCK = "WILL NOT TRANSLATE"
SEV_CHECK = "CHECK AFTER IMPORT"
SEV_INFO = "INFO"

SEV_ORDER = {SEV_BLOCK: 0, SEV_CHECK: 1, SEV_INFO: 2}


# ---------------------------------------------------------------------------
# Timecode helpers
# ---------------------------------------------------------------------------

def rate_of(elem, default_timebase=24, default_ntsc=False):
    """Read a <rate> child (timebase + ntsc) from an element, with fallback."""
    if elem is None:
        return default_timebase, default_ntsc
    rate = elem.find("rate")
    if rate is None:
        return default_timebase, default_ntsc
    tb = rate.findtext("timebase")
    ntsc = (rate.findtext("ntsc") or "").strip().upper() == "TRUE"
    try:
        return int(tb), ntsc
    except (TypeError, ValueError):
        return default_timebase, default_ntsc


def frames_to_tc(frames, timebase, drop_frame=False):
    """Convert a frame count to a timecode string."""
    if frames is None:
        return "--:--:--:--"
    frames = int(frames)
    sign = "-" if frames < 0 else ""
    frames = abs(frames)
    if drop_frame and timebase in (30, 60):
        # Drop-frame math for 29.97 / 59.94
        drop = 2 if timebase == 30 else 4
        fpm = timebase * 60 - drop          # frames per drop-minute
        fp10 = timebase * 600 - drop * 9    # frames per 10 minutes
        d10 = frames // fp10
        rem = frames % fp10
        if rem < timebase * 60:
            extra = d10 * drop * 9
        else:
            extra = d10 * drop * 9 + drop * ((rem - drop) // fpm)
        frames += extra
        sep = ";"
    else:
        sep = ":"
    ff = frames % timebase
    ss = (frames // timebase) % 60
    mm = (frames // (timebase * 60)) % 60
    hh = frames // (timebase * 3600)
    return f"{sign}{hh:02d}:{mm:02d}:{ss:02d}{sep}{ff:02d}"


def int_text(elem, path, default=None):
    if elem is None:
        return default
    txt = elem.findtext(path)
    if txt is None:
        return default
    try:
        return int(txt)
    except ValueError:
        try:
            return int(float(txt))
        except ValueError:
            return default


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Flag:
    severity: str
    category: str
    track: str
    start_frame: int
    tc: str
    clip: str
    detail: str


@dataclass
class SequenceReport:
    name: str
    timebase: int
    ntsc: bool
    drop_frame: bool
    duration_frames: int
    start_offset: int
    video_tracks: int = 0
    audio_tracks: int = 0
    video_clips: int = 0
    audio_clips: int = 0
    markers: int = 0
    effect_inventory: Counter = field(default_factory=Counter)
    transition_inventory: Counter = field(default_factory=Counter)
    flags: list = field(default_factory=list)

    @property
    def fps_label(self):
        if self.ntsc:
            return f"{self.timebase * 1000 / 1001:.3f}"
        return str(self.timebase)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def clip_file_path(clipitem, file_map=None):
    f = clipitem.find("file")
    if f is None:
        return None
    pathurl = f.findtext("pathurl")
    if not pathurl and file_map is not None:
        # FCP7 XML defines each <file> fully once, then references it by id
        pathurl = file_map.get(f.get("id"))
    if not pathurl:
        return None
    path = urllib.parse.unquote(pathurl)
    return re.sub(r"^file://(localhost)?", "", path)


def effect_names(clipitem):
    """Yield (name, effectid) for each filter effect on a clip item."""
    for filt in clipitem.findall("filter"):
        eff = filt.find("effect")
        if eff is None:
            continue
        yield (eff.findtext("name") or "", eff.findtext("effectid") or ""), filt


def keyframe_count(filt):
    return len(filt.findall(".//keyframe"))


def analyze_clipitem(clip, track_label, seq, report, file_map):
    name = clip.findtext("name") or "(unnamed)"
    start = int_text(clip, "start")
    end = int_text(clip, "end")
    cin = int_text(clip, "in")
    cout = int_text(clip, "out")
    if start is None or start < 0:
        # Clips under a transition report start/end of -1; fall back to in/out
        start = int_text(clip, "start", 0)
        start = max(start, 0)
    tc = frames_to_tc((start or 0) + report.start_offset, report.timebase, report.drop_frame)

    def flag(severity, category, detail):
        report.flags.append(Flag(severity, category, track_label, start or 0, tc, name, detail))

    enabled = (clip.findtext("enabled") or "TRUE").strip().upper()
    if enabled == "FALSE":
        flag(SEV_INFO, "Disabled clip",
             "Disabled in Premiere; Resolve may import it enabled or drop it. "
             "Best practice: delete disabled clips before export.")

    # --- Nested sequence / multicam ---
    if clip.find("sequence") is not None:
        flag(SEV_BLOCK, "Nested sequence",
             "Nests/multicam do not conform reliably. Flatten the nest (or replace "
             "with the rendered clip) in Premiere before export.")

    # --- Offline / missing file ---
    f = clip.find("file")
    path = clip_file_path(clip, file_map)
    if f is not None and path is None and clip.find("sequence") is None:
        flag(SEV_CHECK, "No media path",
             "Clip has no file path in the XML (offline, merged clip, or generated "
             "media). Expect it offline in Resolve.")

    # --- Stills / graphics ---
    if path:
        ext = os.path.splitext(path)[1].lower()
        if ext in STILL_EXTENSIONS:
            flag(SEV_CHECK, "Still image",
                 f"Still ({ext}). Verify duration and any motion keyframes after import; "
                 "check Resolve's standard still duration doesn't override it.")
        if ext == ".psd":
            flag(SEV_CHECK, "Photoshop file",
                 "Layered PSDs import flattened in Resolve; if Premiere used a single "
                 "layer, re-export that layer as PNG/TIFF.")
    lowname = name.lower()
    if any(h in lowname for h in GRAPHIC_HINTS) or (path and "mogrt" in path.lower()):
        flag(SEV_BLOCK, "Title / graphic",
             "Titles and Motion Graphics templates do not translate. Rebuild as Text+ "
             "in Resolve, or export from Premiere as ProRes 4444 with alpha and cut in.")

    # --- Speed changes ---
    if start is not None and end is not None and cin is not None and cout is not None \
            and cin >= 0 and cout >= 0 and end > start:
        src_len = cout - cin
        rec_len = end - start
        if src_len != rec_len and src_len > 0 and rec_len > 0:
            speed = 100.0 * src_len / rec_len
            flag(SEV_CHECK, "Constant speed change",
                 f"~{speed:.1f}% speed. Constant speed usually survives but is a known "
                 "source of one-frame drift; verify cut points against the Premiere ref.")
    if cin is not None and cout is not None and cin == cout and cin >= 0:
        flag(SEV_BLOCK, "Freeze frame",
             "Freeze frames often import offline or at the wrong frame. Rebuild in "
             "Resolve (Retime Controls > Freeze Frame) or export a still.")

    # --- Filters / effects ---
    for (ename, eid), filt in effect_names(clip):
        label = ename or eid
        low = f"{ename} {eid}".lower()
        report.effect_inventory[label] += 1
        matched = False
        for key, remedy in EFFECTS_NOT_TRANSLATED.items():
            if key in low:
                if "time" in key and "remap" in key.replace(" ", ""):
                    kf = keyframe_count(filt)
                    detail = (f"Speed ramp ({kf} keyframes). " if kf > 1
                              else "Time remap applied. ") + remedy
                    flag(SEV_BLOCK, "Time remap / speed ramp", detail)
                else:
                    flag(SEV_BLOCK, f"Effect: {label}", remedy)
                matched = True
                break
        if matched:
            continue
        kf = keyframe_count(filt)
        base = low.strip()
        if any(ok in base for ok in EFFECTS_USUALLY_OK):
            if kf > 1:
                flag(SEV_CHECK, f"Keyframed {label}",
                     f"{kf} keyframes. Basic motion/opacity/volume keyframes usually "
                     "translate; spot-check the animation.")
        else:
            flag(SEV_CHECK, f"Effect: {label}",
                 "Not a known-safe effect; likely dropped or altered on import. "
                 "Note the look and plan to rebuild in Resolve.")


def analyze_track(track, kind, index, seq, report, file_map):
    label = f"{kind}{index}"
    clips = track.findall("clipitem")
    if kind == "V":
        report.video_clips += len(clips)
    else:
        report.audio_clips += len(clips)

    for clip in clips:
        analyze_clipitem(clip, label, seq, report, file_map)

    for trans in track.findall("transitionitem"):
        eff = trans.find("effect")
        tname = (eff.findtext("name") if eff is not None else None) or "(unknown transition)"
        report.transition_inventory[tname] += 1
        start = int_text(trans, "start", 0)
        tc = frames_to_tc(start + report.start_offset, report.timebase, report.drop_frame)
        if tname.strip().lower() not in TRANSITION_OK:
            report.flags.append(Flag(
                SEV_CHECK, f"Transition: {tname}", label, start, tc, tname,
                "Non-dissolve transitions import as a plain cross dissolve or are "
                "dropped. Recreate in Resolve if the specific wipe/effect matters."))

    # Gap detection on V1 only (a gap on V1 is usually a mistake in a doc cut)
    if kind == "V" and index == 1 and clips:
        spans = sorted(
            (int_text(c, "start"), int_text(c, "end")) for c in clips
            if int_text(c, "start") is not None and int_text(c, "start") >= 0
        )
        prev_end = None
        for s, e in spans:
            if prev_end is not None and s > prev_end:
                tc = frames_to_tc(prev_end + report.start_offset, report.timebase,
                                  report.drop_frame)
                report.flags.append(Flag(
                    SEV_INFO, "Gap on V1", label, prev_end, tc, "(gap)",
                    f"{s - prev_end} frame gap. Intentional black, or a hole?"))
            prev_end = max(prev_end or 0, e or 0)


def analyze_sequence(seq):
    name = seq.findtext("name") or "(unnamed sequence)"
    timebase, ntsc = rate_of(seq)
    tc_elem = seq.find("timecode")
    drop = False
    start_offset = 0
    if tc_elem is not None:
        drop = (tc_elem.findtext("displayformat") or "NDF").strip().upper() == "DF"
        start_offset = int_text(tc_elem, "frame", 0) or 0
    duration = int_text(seq, "duration", 0) or 0

    report = SequenceReport(
        name=name, timebase=timebase, ntsc=ntsc, drop_frame=drop,
        duration_frames=duration, start_offset=start_offset,
    )

    media = seq.find("media")
    if media is None:
        return report

    file_map = {}
    for f in seq.iter("file"):
        fid = f.get("id")
        pathurl = f.findtext("pathurl")
        if fid and pathurl and fid not in file_map:
            file_map[fid] = pathurl

    for kind, node in (("V", media.find("video")), ("A", media.find("audio"))):
        if node is None:
            continue
        tracks = node.findall("track")
        if kind == "V":
            report.video_tracks = len(tracks)
        else:
            report.audio_tracks = len(tracks)
        for i, track in enumerate(tracks, 1):
            analyze_track(track, kind, i, seq, report, file_map)

    # Mixed frame rates: compare each file's rate to the sequence rate
    seen_files = {}
    for clip in media.iter("clipitem"):
        f = clip.find("file")
        if f is None:
            continue
        fid = f.get("id") or id(f)
        if fid in seen_files:
            continue
        seen_files[fid] = True
        ftb, fntsc = rate_of(f, timebase, ntsc)
        if (ftb, fntsc) != (timebase, ntsc):
            fname = f.findtext("name") or clip.findtext("name") or "(file)"
            flabel = f"{ftb * 1000 / 1001:.3f}" if fntsc else str(ftb)
            start = int_text(clip, "start", 0) or 0
            report.flags.append(Flag(
                SEV_CHECK, "Frame rate mismatch", "-", start,
                frames_to_tc(start + start_offset, timebase, drop), fname,
                f"Source is {flabel} fps in a {report.fps_label} fps timeline. After "
                "import, verify clip attributes in Resolve match Premiere's "
                "'Interpret Footage' setting."))

    report.markers = len(seq.findall("marker")) + len(media.findall(".//marker"))
    report.flags.sort(key=lambda fl: (SEV_ORDER[fl.severity], fl.start_frame))
    return report


def parse_xmeml(path):
    tree = ET.parse(path)
    root = tree.getroot()
    if root.tag != "xmeml":
        raise ValueError(
            f"{path}: not an FCP7 XML (root <{root.tag}>). Export from Premiere with "
            "File > Export > Final Cut Pro XML.")
    seqs = root.findall(".//sequence")
    # Only top-level program sequences: skip sequences nested inside clipitems
    top = [s for s in seqs if not any(s in list(ci) for ci in root.iter("clipitem"))]
    return [analyze_sequence(s) for s in (top or seqs)]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def render_text(report):
    lines = []
    dur_tc = frames_to_tc(report.duration_frames, report.timebase, report.drop_frame)
    lines.append("=" * 78)
    lines.append(f"SEQUENCE: {report.name}")
    lines.append(f"  {report.fps_label} fps ({'DF' if report.drop_frame else 'NDF'})  "
                 f"duration {dur_tc}  "
                 f"V tracks: {report.video_tracks} ({report.video_clips} clips)  "
                 f"A tracks: {report.audio_tracks} ({report.audio_clips} clips)  "
                 f"markers: {report.markers}")
    counts = Counter(fl.severity for fl in report.flags)
    lines.append(f"  Flags: {counts.get(SEV_BLOCK, 0)} blocking, "
                 f"{counts.get(SEV_CHECK, 0)} to check, {counts.get(SEV_INFO, 0)} info")
    lines.append("=" * 78)
    current = None
    for fl in report.flags:
        if fl.severity != current:
            current = fl.severity
            lines.append(f"\n--- {current} ---")
        lines.append(f"[{fl.tc}] {fl.track:<3} {fl.clip}")
        lines.append(f"    {fl.category}: {fl.detail}")
    if not report.flags:
        lines.append("\nNo flags. Clean export -- still run the timeline compare after import.")
    if report.transition_inventory:
        lines.append("\nTransitions used: " + ", ".join(
            f"{n} x{c}" for n, c in report.transition_inventory.most_common()))
    if report.effect_inventory:
        lines.append("Effects used:     " + ", ".join(
            f"{n} x{c}" for n, c in report.effect_inventory.most_common()))
    return "\n".join(lines)


def render_markdown(reports, source):
    out = [f"# Pre-flight report: `{os.path.basename(source)}`", ""]
    for r in reports:
        counts = Counter(fl.severity for fl in r.flags)
        out += [
            f"## {r.name}",
            "",
            f"- **{r.fps_label} fps** ({'DF' if r.drop_frame else 'NDF'}), "
            f"duration {frames_to_tc(r.duration_frames, r.timebase, r.drop_frame)}",
            f"- Video: {r.video_tracks} tracks / {r.video_clips} clips - "
            f"Audio: {r.audio_tracks} tracks / {r.audio_clips} clips - "
            f"Markers: {r.markers}",
            f"- Flags: **{counts.get(SEV_BLOCK, 0)} blocking**, "
            f"{counts.get(SEV_CHECK, 0)} to check, {counts.get(SEV_INFO, 0)} info",
            "",
        ]
        if r.flags:
            out += ["| TC | Track | Clip | Severity | Issue | What to do |",
                    "|---|---|---|---|---|---|"]
            for fl in r.flags:
                clip = fl.clip.replace("|", "\\|")
                detail = fl.detail.replace("|", "\\|")
                out.append(f"| `{fl.tc}` | {fl.track} | {clip} | {fl.severity} | "
                           f"{fl.category} | {detail} |")
            out.append("")
        else:
            out += ["No flags found.", ""]
    return "\n".join(out)


def write_csv(reports, source, csv_path):
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["source_xml", "sequence", "timecode", "track", "clip",
                    "severity", "category", "detail"])
        for r in reports:
            for fl in r.flags:
                w.writerow([os.path.basename(source), r.name, fl.tc, fl.track,
                            fl.clip, fl.severity, fl.category, fl.detail])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("xml", nargs="+", help="FCP7 XML file(s) exported from Premiere")
    ap.add_argument("--md", help="write a markdown report to this path (single input)")
    ap.add_argument("--md-dir", help="write one markdown report per input XML here")
    ap.add_argument("--csv", help="write all flags to this CSV")
    args = ap.parse_args(argv)

    all_reports = []
    exit_code = 0
    for path in args.xml:
        try:
            reports = parse_xmeml(path)
        except (ET.ParseError, ValueError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            exit_code = 2
            continue
        all_reports.append((path, reports))
        for r in reports:
            print(render_text(r))
            print()
            if any(fl.severity == SEV_BLOCK for fl in r.flags):
                exit_code = max(exit_code, 1)
        if args.md and len(args.xml) == 1:
            with open(args.md, "w", encoding="utf-8") as fh:
                fh.write(render_markdown(reports, path))
            print(f"Markdown report -> {args.md}")
        if args.md_dir:
            os.makedirs(args.md_dir, exist_ok=True)
            base = os.path.splitext(os.path.basename(path))[0]
            dest = os.path.join(args.md_dir, base + "_preflight.md")
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(render_markdown(reports, path))
            print(f"Markdown report -> {dest}")

    if args.csv and all_reports:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["source_xml", "sequence", "timecode", "track", "clip",
                        "severity", "category", "detail"])
            for path, reports in all_reports:
                for r in reports:
                    for fl in r.flags:
                        w.writerow([os.path.basename(path), r.name, fl.tc, fl.track,
                                    fl.clip, fl.severity, fl.category, fl.detail])
        print(f"CSV flags -> {args.csv}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
