#!/usr/bin/env python3
"""
SLYBURN one-command conform check: Premiere XML in, punch list out.

Give it ONE file (the XML exported from Premiere) and it scrapes the
sequence for everything that won't survive the trip into DaVinci Resolve
-- speed ramps, nests/multicams, titles, freeze frames, odd transitions,
mixed frame rates, offline media -- with timecodes.

Give it TWO files (the Premiere XML, then the XML exported back out of
Resolve after import) and it ALSO diffs the timelines clip-by-clip:
missing clips, extra clips, shifted cuts, and clips showing different
frames of the media. Clean compare = the conform is verified.

    python3 slyburn_check.py SEQ01_premiere.xml
    python3 slyburn_check.py SEQ01_premiere.xml SEQ01_resolve.xml
    python3 slyburn_check.py SEQ01_premiere.xml SEQ01_resolve.xml --tolerance 1

How to make the two files:
  Premiere:  File > Export > Final Cut Pro XML...
  Resolve:   import that XML (File > Import > Timeline), then right-click
             the timeline in the Media Pool > Timelines > Export >
             Final Cut Pro 7 XML...

Reports are written next to the Premiere XML:
  <name>_preflight.md / _flags.csv    what to fix, with timecodes
  <name>_compare.txt                  the Premiere-vs-Resolve diff
"""

import argparse
import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))

import compare_timelines
import preflight_check


class Tee(io.TextIOBase):
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)
        return len(s)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Scrape a Premiere XML for Resolve-migration problems, "
                    "and optionally verify the Resolve import against it.")
    ap.add_argument("premiere_xml", help="XML exported from Premiere")
    ap.add_argument("resolve_xml", nargs="?",
                    help="XML exported from Resolve after import (optional)")
    ap.add_argument("--tolerance", type=int, default=0, metavar="FRAMES",
                    help="ignore timeline drift up to this many frames in "
                         "the compare step")
    args = ap.parse_args(argv)

    base = os.path.splitext(os.path.basename(args.premiere_xml))[0]
    outdir = os.path.dirname(os.path.abspath(args.premiere_xml))
    md_path = os.path.join(outdir, base + "_preflight.md")
    csv_path = os.path.join(outdir, base + "_flags.csv")

    print("=" * 78)
    print("STEP 1 -- scraping the Premiere XML for things that won't survive"
          " the trip")
    print("=" * 78)
    rc_preflight = preflight_check.main(
        [args.premiere_xml, "--md", md_path, "--csv", csv_path])
    if rc_preflight == 2:
        return 2

    rc_compare = None
    if args.resolve_xml:
        print()
        print("=" * 78)
        print("STEP 2 -- comparing the Resolve import against the Premiere"
              " original")
        print("=" * 78)
        buf = io.StringIO()
        with contextlib.redirect_stdout(Tee(sys.stdout, buf)):
            rc_compare = compare_timelines.main(
                [args.premiere_xml, args.resolve_xml,
                 "--tolerance", str(args.tolerance)])
        cmp_path = os.path.join(outdir, base + "_compare.txt")
        with open(cmp_path, "w", encoding="utf-8") as fh:
            fh.write(buf.getvalue())
        print(f"Compare report -> {cmp_path}")

    print()
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)
    if rc_preflight:
        print("* Pre-flight found items that will NOT translate -- fix those "
              "in Premiere (or plan the rebuild in Resolve). Punch list: "
              f"{md_path}")
    else:
        print("* Pre-flight clean: nothing in this sequence is known to break.")
    if rc_compare is None:
        print("* No Resolve XML given yet. After importing into Resolve, "
              "export the timeline back out and run:\n"
              f"    python3 {os.path.basename(sys.argv[0])} "
              f"{args.premiere_xml} <resolve export>.xml")
    elif rc_compare:
        print("* Timelines DO NOT MATCH -- fix the discrepancies above in "
              "Resolve, re-export, re-run.")
    else:
        print("* Timelines MATCH. Conform verified -- sign it off in "
              "CHECKLIST.md.")
    return max(rc_preflight, rc_compare or 0)


if __name__ == "__main__":
    sys.exit(main())
