#!/usr/bin/env python3
"""
DaVinci Resolve automation for the Slyburn Premiere -> Resolve migration.

Runs on the workstation where DaVinci Resolve (Studio or free) is installed
and RUNNING, using Resolve's built-in scripting API. Enable it once in
Resolve: Preferences > System > General > External scripting using > Local.

Subcommands:
  import  Batch-import one or more FCP7 XMLs as new timelines
  export  Export timelines back to FCP7 XML (for compare_timelines.py)
  audit   List offline/unlinked clips and per-track clip counts
          for a timeline (or all timelines)

Examples:
  python3 resolve_conform.py import exports/*.xml
  python3 resolve_conform.py export --all --out roundtrip/
  python3 resolve_conform.py audit --timeline "SLY_EP1_SEQ03"

Environment (macOS defaults shown; see README for Windows paths):
  export RESOLVE_SCRIPT_API="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
  export RESOLVE_SCRIPT_LIB="/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
  export PYTHONPATH="$PYTHONPATH:$RESOLVE_SCRIPT_API/Modules/"
"""

import argparse
import os
import sys


def connect():
    try:
        import DaVinciResolveScript as dvr
    except ImportError:
        sys.exit(
            "Could not import DaVinciResolveScript.\n"
            "1) Is Resolve running?\n"
            "2) Preferences > System > General > External scripting using: Local\n"
            "3) Set RESOLVE_SCRIPT_API / RESOLVE_SCRIPT_LIB / PYTHONPATH "
            "(see module docstring / README)."
        )
    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        sys.exit("Connected to the module but not to Resolve. Is Resolve running?")
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject()
    if project is None:
        sys.exit("No project open in Resolve. Open the Slyburn project first.")
    return resolve, project


# Import settings tuned for a conform: don't let Resolve silently invent
# things; bring source clips in so relinking is explicit and visible.
IMPORT_OPTIONS = {
    "timelineFrameRate": None,          # set per-XML if --fps given
    "importSourceClips": True,          # add media referenced by the XML
    "sourceClipsPath": "",              # filled by --media-root
}


def cmd_import(project, args):
    media_pool = project.GetMediaPool()
    ok, failed = [], []
    for xml_path in args.xml:
        xml_path = os.path.abspath(xml_path)
        if not os.path.exists(xml_path):
            print(f"  SKIP (not found): {xml_path}")
            failed.append(xml_path)
            continue
        options = {
            "importSourceClips": not args.no_source_clips,
        }
        if args.media_root:
            options["sourceClipsPath"] = os.path.abspath(args.media_root)
        timeline = media_pool.ImportTimelineFromFile(xml_path, options)
        if timeline:
            name = timeline.GetName()
            print(f"  OK: {os.path.basename(xml_path)} -> timeline '{name}'")
            ok.append(name)
        else:
            print(f"  FAILED: {os.path.basename(xml_path)}")
            failed.append(xml_path)
    print(f"\nImported {len(ok)}/{len(ok) + len(failed)} timelines.")
    if ok:
        print("Now run:  python3 resolve_conform.py audit  "
              "to check for offline clips, then export for comparison.")
    return 1 if failed else 0


def iter_timelines(project, only=None):
    count = project.GetTimelineCount()
    for i in range(1, count + 1):
        tl = project.GetTimelineByIndex(i)
        if tl is None:
            continue
        if only and tl.GetName() not in only:
            continue
        yield tl


def cmd_export(resolve, project, args):
    os.makedirs(args.out, exist_ok=True)
    names = None
    if not args.all:
        if not args.timeline:
            sys.exit("Pass --timeline NAME (repeatable) or --all.")
        names = set(args.timeline)
    exported = 0
    for tl in iter_timelines(project, names):
        dest = os.path.join(args.out, tl.GetName() + "_resolve.xml")
        if tl.Export(dest, resolve.EXPORT_FCP_7_XML, resolve.EXPORT_NONE):
            print(f"  OK: {tl.GetName()} -> {dest}")
            exported += 1
        else:
            print(f"  FAILED: {tl.GetName()}")
    print(f"\nExported {exported} timeline(s) to {args.out}/")
    print("Compare each against its Premiere export with compare_timelines.py.")
    return 0 if exported else 1


def cmd_audit(project, args):
    names = set(args.timeline) if args.timeline else None
    any_offline = False
    for tl in iter_timelines(project, names):
        print(f"\nTimeline: {tl.GetName()}  "
              f"({tl.GetStartFrame()}-{tl.GetEndFrame()}, "
              f"{tl.GetSetting('timelineFrameRate')} fps)")
        v_tracks = tl.GetTrackCount("video")
        a_tracks = tl.GetTrackCount("audio")
        offline = []
        for kind, count in (("video", v_tracks), ("audio", a_tracks)):
            for idx in range(1, count + 1):
                items = tl.GetItemListInTrack(kind, idx) or []
                label = f"{'V' if kind == 'video' else 'A'}{idx}"
                print(f"  {label}: {len(items)} clips")
                for item in items:
                    mpi = item.GetMediaPoolItem()
                    if mpi is None:
                        offline.append((label, item.GetStart(), item.GetName(),
                                        "no media pool item (generated/offline)"))
                        continue
                    path = mpi.GetClipProperty("File Path")
                    if not path:
                        offline.append((label, item.GetStart(), item.GetName(),
                                        "no file path (unlinked)"))
                    elif not os.path.exists(path):
                        offline.append((label, item.GetStart(), item.GetName(),
                                        f"file missing on disk: {path}"))
        if offline:
            any_offline = True
            print(f"  !! {len(offline)} offline/unlinked item(s):")
            for label, start, name, why in offline:
                print(f"     {label} @frame {start}: {name} -- {why}")
        else:
            print("  All clips online.")
    return 1 if any_offline else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_imp = sub.add_parser("import", help="batch-import FCP7 XMLs as timelines")
    p_imp.add_argument("xml", nargs="+")
    p_imp.add_argument("--media-root",
                       help="folder to search for source media while importing")
    p_imp.add_argument("--no-source-clips", action="store_true",
                       help="link only against clips already in the media pool")

    p_exp = sub.add_parser("export", help="export timelines to FCP7 XML")
    p_exp.add_argument("--timeline", action="append",
                       help="timeline name (repeatable)")
    p_exp.add_argument("--all", action="store_true", help="export every timeline")
    p_exp.add_argument("--out", default="resolve_roundtrip",
                       help="output directory (default: resolve_roundtrip/)")

    p_aud = sub.add_parser("audit", help="clip counts + offline media report")
    p_aud.add_argument("--timeline", action="append",
                       help="timeline name (repeatable); default: all")

    args = ap.parse_args()
    resolve, project = connect()
    print(f"Connected to Resolve project: {project.GetName()}")

    if args.cmd == "import":
        return cmd_import(project, args)
    if args.cmd == "export":
        return cmd_export(resolve, project, args)
    if args.cmd == "audit":
        return cmd_audit(project, args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
