#!/usr/bin/env python3
"""
Classify timeline clips (dialogue / SOT / performance / NAT / SFX / music,
and A-roll / B-roll / archival / GFX for video) and check that they sit on
the tracks your spec says they should.

Input is a SNAPSHOT JSON from slyburn_premiere_toolkit.jsx (or a Resolve
inventory in the same shape). Classification is metadata-based: Claude and
this script cannot listen to audio -- filenames, paths, extensions, track
position, and duration are the signal, and on a well-organized project
that's most of it.

Two stages:
  1. Heuristics (free, offline): spec-driven path hints, name keywords,
     and extension hints classify the obvious clips.
  2. Optional --claude: everything the heuristics couldn't settle is sent
     -- metadata only, in batches -- to the Anthropic API for reasoning.
     Default model: claude-fable-5. Needs `pip install anthropic` and an
     ANTHROPIC_API_KEY (or `ant auth login` profile) on the machine.

Usage:
    python3 clip_classifier.py snapshot.json --spec ../specs/track_spec.json
    python3 clip_classifier.py snapshot.json --spec spec.json --claude
    python3 clip_classifier.py snapshot.json --spec spec.json --claude \
        --md report.md --csv clips.csv

Exit codes: 0 = all clips classified and correctly placed; 1 = misplaced
or unknown clips found; 2 = error.
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter

CONF_ORDER = {"high": 0, "medium": 1, "low": 2}


# ---------------------------------------------------------------------------
# Spec handling
# ---------------------------------------------------------------------------

def load_spec(path):
    with open(path, encoding="utf-8") as fh:
        spec = json.load(fh)
    for key in ("categories", "audio_tracks"):
        if key not in spec:
            raise ValueError(f"spec is missing required key '{key}'")
    return spec


def parse_track_range(label):
    """'A1-A4' -> ('A', 1, 4); 'V3' -> ('V', 3, 3)."""
    m = re.fullmatch(r"([AV])(\d+)(?:-[AV]?(\d+))?", label.strip(), re.I)
    if not m:
        raise ValueError(f"bad track range in spec: {label!r}")
    kind = m.group(1).upper()
    lo = int(m.group(2))
    hi = int(m.group(3)) if m.group(3) else lo
    return kind, lo, hi


def allowed_categories(spec, track):
    """Categories the spec allows on e.g. 'A7'. None = track not in spec."""
    kind = track[0].upper()
    idx = int(track[1:])
    mapping = spec.get("audio_tracks" if kind == "A" else "video_tracks", {})
    for label, cats in mapping.items():
        if label.startswith("_"):
            continue
        k, lo, hi = parse_track_range(label)
        if k == kind and lo <= idx <= hi:
            return cats
    return None


def target_tracks_for(spec, category, kind):
    """Where the spec says this category belongs, e.g. 'A11-A12'."""
    mapping = spec.get("audio_tracks" if kind == "A" else "video_tracks", {})
    return [label for label, cats in mapping.items()
            if not label.startswith("_") and category in cats]


def categories_for_kind(spec, kind):
    key = "categories" if kind == "A" else "video_categories"
    cats = spec.get(key) or spec.get("categories", {})
    return {k: v for k, v in cats.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Stage 1: heuristic classification
# ---------------------------------------------------------------------------

def classify_heuristic(clip, spec):
    """Return (category, confidence, why) or (None, None, None)."""
    kind = clip["track"][0].upper()
    name = (clip.get("name") or "").lower()
    path = (clip.get("mediaPath") or "").lower()
    cats = categories_for_kind(spec, kind)

    # 1. Folder path hints: strongest signal
    for fragment, category in spec.get("path_hints", {}).items():
        if fragment.startswith("_") or category not in cats:
            continue
        if f"/{fragment.lower()}/" in path or f"\\{fragment.lower()}\\" in path:
            return category, "high", f"media path contains /{fragment}/"

    # 2. Name keywords from each category
    for category, info in cats.items():
        for kw in info.get("keywords", []):
            if kw.lower() in name:
                return category, "medium", f"name contains '{kw}'"

    # 3. Extension lean
    ext = os.path.splitext(path or name)[1]
    hint = spec.get("extension_hints", {}).get(ext)
    if hint and hint in cats:
        return hint, "low", f"extension {ext}"

    return None, None, None


# ---------------------------------------------------------------------------
# Stage 2: Claude classification of the leftovers (metadata only)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You classify clips from a documentary film editing timeline into audio/video
categories, using METADATA ONLY (you cannot hear or see the media): clip name,
file path, extension, track, and duration. The film is a feature documentary
about the Van Cliburn International Piano Competition, so live piano
performance material is common and distinct from score music.

You will get the category definitions and a JSON array of clips. For each
clip return its most likely category. Use "UNKNOWN" when the metadata is
genuinely uninformative -- do not guess camera-original names like
A001_C012.mov into a category unless track position or duration makes it
clear. Base rates: long clips on low-numbered audio tracks are usually
speech; very long audio on high tracks is usually music or ambience."""


def build_batch_prompt(batch, cats):
    lines = ["Category definitions:"]
    for key, info in cats.items():
        lines.append(f"- {key}: {info.get('description', '')}")
    lines.append("- UNKNOWN: metadata insufficient to decide.")
    lines.append("\nClips to classify (JSON):")
    lines.append(json.dumps(batch, indent=1))
    lines.append(
        "\nRespond with JSON only, matching this shape exactly:\n"
        '{"classifications": [{"id": <int>, "category": "<one of the keys '
        'above or UNKNOWN>", "confidence": "high|medium|low", '
        '"why": "<one short sentence>"}]}'
    )
    return "\n".join(lines)


def response_schema(cat_keys):
    return {
        "type": "object",
        "properties": {
            "classifications": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "category": {"type": "string",
                                     "enum": list(cat_keys) + ["UNKNOWN"]},
                        "confidence": {"type": "string",
                                       "enum": ["high", "medium", "low"]},
                        "why": {"type": "string"},
                    },
                    "required": ["id", "category", "confidence", "why"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["classifications"],
        "additionalProperties": False,
    }


def call_claude(client, model, prompt, schema, effort):
    """One classification request. Returns the parsed JSON object."""
    import anthropic

    kwargs = dict(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        output_config={"effort": effort,
                       "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        # Fable 5's safety classifiers can decline; server-side fallback
        # re-runs a declined request on another model automatically.
        response = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            **kwargs,
        )
    except TypeError as exc:
        if "keyword" not in str(exc):
            raise  # not the old-SDK unknown-parameter case
        # Older SDK without the fallbacks parameter -- plain call.
        response = client.messages.create(**kwargs)

    if response.stop_reason == "refusal":
        raise RuntimeError("The API declined this request (stop_reason=refusal). "
                           "Clip metadata should never trigger this -- re-run, "
                           "or classify the remaining clips by hand.")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def classify_with_claude(unresolved, spec, model, effort, batch_size=40):
    """Classify leftover clips via the Anthropic API. Mutates results in place."""
    try:
        import anthropic
    except ImportError:
        sys.exit("--claude needs the SDK on this machine:  pip install anthropic")

    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY or `ant auth login` profile
    if not (getattr(client, "api_key", None) or getattr(client, "auth_token", None)):
        sys.exit("No Anthropic credentials found. Set ANTHROPIC_API_KEY (from "
                 "console.anthropic.com) or run `ant auth login`, then re-run.")

    by_kind = {"A": [], "V": []}
    for item in unresolved:
        by_kind[item["clip"]["track"][0].upper()].append(item)

    for kind, items in by_kind.items():
        if not items:
            continue
        cats = categories_for_kind(spec, kind)
        schema = response_schema(cats.keys())
        for start in range(0, len(items), batch_size):
            chunk = items[start:start + batch_size]
            batch_payload = []
            for i, item in enumerate(chunk):
                c = item["clip"]
                batch_payload.append({
                    "id": i,
                    "name": c.get("name"),
                    "path": c.get("mediaPath") or None,
                    "track": c["track"],
                    "durationSeconds": round(
                        c.get("endSeconds", 0) - c.get("startSeconds", 0), 1),
                })
            print(f"  Claude ({model}): classifying {len(chunk)} "
                  f"{'audio' if kind == 'A' else 'video'} clip(s)...")
            try:
                result = call_claude(client, model,
                                     build_batch_prompt(batch_payload, cats),
                                     schema, effort)
            except anthropic.AuthenticationError:
                sys.exit("No valid Anthropic credentials. Set ANTHROPIC_API_KEY "
                         "or run `ant auth login`, then re-run.")
            for row in result.get("classifications", []):
                idx = row.get("id")
                if not isinstance(idx, int) or not 0 <= idx < len(chunk):
                    continue
                cat = row.get("category", "UNKNOWN")
                if cat != "UNKNOWN" and cat not in cats:
                    cat = "UNKNOWN"
                chunk[idx]["category"] = cat
                chunk[idx]["confidence"] = row.get("confidence", "low")
                chunk[idx]["why"] = "Claude: " + row.get("why", "")


# ---------------------------------------------------------------------------
# Spec conformance + reporting
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("snapshot", help="SNAPSHOT JSON from the Premiere toolkit")
    ap.add_argument("--spec", required=True, help="track spec JSON")
    ap.add_argument("--claude", action="store_true",
                    help="send unresolved clips (metadata only) to the "
                         "Anthropic API for classification")
    ap.add_argument("--model", default="claude-fable-5",
                    help="model for --claude (default: claude-fable-5; "
                         "claude-opus-5 is a cheaper alternative)")
    ap.add_argument("--effort", default="low",
                    choices=["low", "medium", "high"],
                    help="reasoning effort for --claude (default: low -- "
                         "plenty for metadata classification)")
    ap.add_argument("--audio-only", action="store_true",
                    help="skip video clips")
    ap.add_argument("--md", help="write a markdown report here")
    ap.add_argument("--csv", help="write per-clip results here")
    args = ap.parse_args(argv)

    try:
        with open(args.snapshot, encoding="utf-8") as fh:
            doc = json.load(fh)
        spec = load_spec(args.spec)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    clips = doc.get("clips", [])
    if args.audio_only:
        clips = [c for c in clips if c["track"].startswith("A")]
    if not spec.get("video_tracks"):
        clips = [c for c in clips if not c["track"].startswith("V")]
    print(f"Sequence: {doc.get('sequence')}  --  {len(clips)} clip(s) to classify")

    # Stage 1
    results = []
    unresolved = []
    for clip in clips:
        category, conf, why = classify_heuristic(clip, spec)
        item = {"clip": clip, "category": category, "confidence": conf,
                "why": why}
        results.append(item)
        if category is None:
            unresolved.append(item)
    print(f"Heuristics: {len(results) - len(unresolved)} classified, "
          f"{len(unresolved)} unresolved")

    # Stage 2
    if unresolved and args.claude:
        classify_with_claude(unresolved, spec, args.model, args.effort)
    for item in results:
        if item["category"] is None:
            item["category"] = "UNKNOWN"
            item["confidence"] = "low"
            item["why"] = item["why"] or "no heuristic match" + (
                "" if args.claude else " (re-run with --claude to resolve)")

    # Spec conformance
    misplaced, unknown, ok = [], [], []
    for item in results:
        clip = item["clip"]
        track = clip["track"]
        cat = item["category"]
        if cat == "UNKNOWN":
            unknown.append(item)
            continue
        allowed = allowed_categories(spec, track)
        if allowed is None:
            item["placement"] = f"track {track} not covered by spec"
            misplaced.append(item)
        elif cat not in allowed:
            targets = target_tracks_for(spec, cat, track[0].upper())
            item["placement"] = (f"{cat} does not belong on {track} "
                                 f"(spec: {track} = {'/'.join(allowed)}); "
                                 f"move to {', '.join(targets) or '??'}")
            misplaced.append(item)
        else:
            ok.append(item)

    # Report
    counts = Counter(i["category"] for i in results)
    print("\nCategory totals: " +
          ", ".join(f"{c} x{n}" for c, n in counts.most_common()))
    print(f"Placement: {len(ok)} OK, {len(misplaced)} MISPLACED, "
          f"{len(unknown)} unknown")

    def line(item):
        c = item["clip"]
        return (f"  [{c.get('tc', '?')}] {c['track']:<4} {c.get('name')}  "
                f"-> {item['category']} ({item['confidence']}; {item['why']})")

    if misplaced:
        print("\n--- MISPLACED (fix before/after conform) ---")
        for item in sorted(misplaced, key=lambda i: i["clip"].get("startSeconds", 0)):
            print(line(item))
            print(f"      {item['placement']}")
    if unknown:
        print("\n--- UNKNOWN (needs a human ear) ---")
        for item in sorted(unknown, key=lambda i: i["clip"].get("startSeconds", 0)):
            print(line(item))

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["tc", "track", "clip", "mediaPath", "category",
                        "confidence", "why", "placement"])
            for item in results:
                c = item["clip"]
                w.writerow([c.get("tc"), c["track"], c.get("name"),
                            c.get("mediaPath"), item["category"],
                            item["confidence"], item["why"],
                            item.get("placement", "OK")])
        print(f"\nCSV -> {args.csv}")

    if args.md:
        out = [f"# Track-spec check: {doc.get('sequence')}", "",
               f"- {len(ok)} correctly placed, **{len(misplaced)} misplaced**, "
               f"{len(unknown)} unknown", ""]
        if misplaced:
            out += ["## Misplaced", "",
                    "| TC | Track | Clip | Category | Fix |", "|---|---|---|---|---|"]
            for item in misplaced:
                c = item["clip"]
                out.append(f"| `{c.get('tc')}` | {c['track']} | "
                           f"{str(c.get('name')).replace('|', '/')} | "
                           f"{item['category']} | {item['placement']} |")
            out.append("")
        if unknown:
            out += ["## Unknown", ""]
            out += [f"- `{i['clip'].get('tc')}` {i['clip']['track']} "
                    f"{i['clip'].get('name')}" for i in unknown]
            out.append("")
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out))
        print(f"Markdown -> {args.md}")

    return 1 if (misplaced or unknown) else 0


if __name__ == "__main__":
    sys.exit(main())
