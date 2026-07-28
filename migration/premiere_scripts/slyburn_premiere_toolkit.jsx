/*
 * SLYBURN Premiere Pro toolkit (ExtendScript, ES3)
 * ------------------------------------------------
 * Run against the ACTIVE SEQUENCE. Tools:
 *
 *   1. AUDIT      - drop colored sequence markers + write a report on:
 *                   multicam clips (classified: real vs will-break-on-flatten),
 *                   time remaps / speed changes, disabled clips, nests.
 *   2. SNAPSHOT   - dump the timeline to JSON (run before AND after you
 *                   flatten in the UI, then diff with compare_snapshots.py
 *                   to catch clips that silently disappeared).
 *   3. MOTION     - export Position/Scale/Rotation/Opacity per clip to a
 *                   JSON sidecar (XML loses these; apply_motion_sidecar.py
 *                   re-applies them in Resolve).
 *   4. RECONSTRUCT- for multicams/nests that flatten to nothing: drill in,
 *                   do the in/out offset math, and overwrite the true source
 *                   clip onto the topmost (empty) video track, then verify.
 *   5. CLEAN      - delete disabled clips (writes a log of every removal).
 *
 * Marker colors used by AUDIT:
 *   Red    = multicam/nest that will BREAK on flatten (or is opaque)
 *   Blue   = real multicam (flatten via UI is fine)
 *   Orange = time remap (speed ramp)
 *   Yellow = constant speed change
 *   Purple = disabled clip
 *   Cyan   = OFFLINE media (also writes <seq>_offline_media.txt hit-list)
 *   Green  = reconstruction placed + verified
 *
 * Reports are written next to the .prproj in  slyburn_reports/.
 *
 * NOTE: Premiere's API cannot trigger the Flatten command itself, and
 * cannot add tracks reliably -- before running RECONSTRUCT, add one empty
 * video track at the top of the sequence (Sequence > Add Tracks).
 */

/* eslint-disable */

var TICKS_PER_SECOND = 254016000000;

// ---------------------------------------------------------------- utilities

function seqFps(seq) {
    try {
        var tb = parseFloat(seq.timebase); // ticks per frame
        if (tb > 0) return TICKS_PER_SECOND / tb;
    } catch (e) {}
    try {
        var s = seq.getSettings();
        var t = parseFloat(s.videoFrameRate.ticks);
        if (t > 0) return TICKS_PER_SECOND / t;
    } catch (e2) {}
    return 23.976;
}

function seqZeroSeconds(seq) {
    try { return parseFloat(seq.zeroPoint) / TICKS_PER_SECOND; } catch (e) {}
    return 0;
}

function toTC(seconds, fps, zeroSeconds) {
    var total = Math.round((seconds + (zeroSeconds || 0)) * fps);
    var fpsI = Math.round(fps);
    if (fpsI < 1) fpsI = 24;
    var f = total % fpsI;
    var s = Math.floor(total / fpsI) % 60;
    var m = Math.floor(total / (fpsI * 60)) % 60;
    var h = Math.floor(total / (fpsI * 3600));
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return p(h) + ":" + p(m) + ":" + p(s) + ":" + p(f);
}

function jsonEsc(s) {
    if (s === null || s === undefined) return "";
    s = String(s);
    var out = "";
    for (var i = 0; i < s.length; i++) {
        var c = s.charAt(i);
        if (c === '"' || c === "\\") out += "\\" + c;
        else if (c === "\n") out += "\\n";
        else if (c === "\r") out += "\\r";
        else if (c === "\t") out += "\\t";
        else out += c;
    }
    return out;
}

// Minimal JSON serializer (ExtendScript has no JSON global).
function toJson(v, indent) {
    indent = indent || "";
    var nl = "\n" + indent + "  ";
    var i, parts;
    if (v === null || v === undefined) return "null";
    if (typeof v === "number") return isFinite(v) ? String(v) : "null";
    if (typeof v === "boolean") return v ? "true" : "false";
    if (typeof v === "string") return '"' + jsonEsc(v) + '"';
    if (v instanceof Array) {
        parts = [];
        for (i = 0; i < v.length; i++) parts.push(toJson(v[i], indent + "  "));
        if (!parts.length) return "[]";
        return "[" + nl + parts.join("," + nl) + "\n" + indent + "]";
    }
    parts = [];
    for (var k in v) {
        if (v.hasOwnProperty(k)) {
            parts.push('"' + jsonEsc(k) + '": ' + toJson(v[k], indent + "  "));
        }
    }
    if (!parts.length) return "{}";
    return "{" + nl + parts.join("," + nl) + "\n" + indent + "}";
}

function reportsFolder() {
    var base = null;
    try {
        if (app.project.path) base = new File(app.project.path).parent;
    } catch (e) {}
    if (!base || !base.exists) base = Folder.desktop;
    var f = new Folder(base.fsName + "/slyburn_reports");
    if (!f.exists) f.create();
    return f;
}

function writeTextFile(name, content) {
    var f = new File(reportsFolder().fsName + "/" + name);
    f.encoding = "UTF-8";
    if (f.open("w")) { f.write(content); f.close(); return f.fsName; }
    return null;
}

function safeName(s) {
    return String(s).replace(/[^A-Za-z0-9_\-]+/g, "_");
}

function addMarker(seq, seconds, name, comment, colorIndex) {
    try {
        var m = seq.markers.createMarker(seconds);
        m.name = name;
        try { m.comments = comment; } catch (e1) {}
        try { m.setColorByIndex(colorIndex); } catch (e2) {}
        return true;
    } catch (e) { return false; }
}

var MARKER = { GREEN: 0, RED: 1, PURPLE: 2, ORANGE: 3, YELLOW: 4, BLUE: 6, CYAN: 7 };

// ------------------------------------------------------------- clip probing

function piKind(pi) {
    var k = { multicam: false, merged: false, sequence: false, path: "",
              offline: false };
    if (!pi) return k;
    try { k.multicam = pi.isMulticamClip && pi.isMulticamClip(); } catch (e1) {}
    try { k.merged = pi.isMergedClip && pi.isMergedClip(); } catch (e2) {}
    try { k.sequence = pi.isSequence && pi.isSequence(); } catch (e3) {}
    try { k.path = pi.getMediaPath ? (pi.getMediaPath() || "") : ""; } catch (e4) {}
    try { k.offline = pi.isOffline && pi.isOffline() ? true : false; } catch (e5) {}
    return k;
}

function clipSpeedInfo(clip) {
    var info = { speed: 1, reversed: false, timeRemap: false, remapKeys: 0 };
    try { info.speed = clip.getSpeed(); } catch (e1) {}
    try { info.reversed = clip.isSpeedReversed && clip.isSpeedReversed() ? true : false; } catch (e2) {}
    try {
        var comps = clip.components;
        for (var i = 0; i < comps.numItems; i++) {
            var c = comps[i];
            var dn = String(c.displayName || "");
            var mn = String(c.matchName || "");
            if (dn.toLowerCase().indexOf("remap") >= 0 ||
                mn.toLowerCase().indexOf("timeremap") >= 0 ||
                mn.toLowerCase().indexOf("time remapping") >= 0) {
                for (var j = 0; j < c.properties.numItems; j++) {
                    var p = c.properties[j];
                    var varying = false;
                    try { varying = p.isTimeVarying(); } catch (e3) {}
                    if (varying) {
                        info.timeRemap = true;
                        try { info.remapKeys = p.getKeys().length; } catch (e4) {}
                    }
                }
            }
        }
    } catch (e5) {}
    return info;
}

// Find the Sequence object backing a (multicam/nested) projectItem.
function findInnerSequence(pi) {
    if (!pi) return null;
    try {
        var seqs = app.project.sequences;
        for (var i = 0; i < seqs.numSequences; i++) {
            var s = seqs[i];
            try {
                if (s.projectItem && s.projectItem.nodeId === pi.nodeId) return s;
            } catch (e1) {}
        }
    } catch (e2) {}
    return null;
}

// Classify a multicam/nested clip by looking inside its source sequence.
//   REAL_MULTICAM   : >1 parallel angle, all plain media -> flatten in UI
//   RECONSTRUCTABLE : resolves to one plain source clip  -> tool 4 fixes it
//   WILL_BREAK      : inner clips are nests/merged clips -> flatten loses media
//   OPAQUE          : can't see inside                    -> manual
function classifyContainer(outerClip) {
    var pi = outerClip.projectItem;
    var inner = findInnerSequence(pi);
    var result = { cls: "OPAQUE", innerClips: [], detail: "" };
    if (!inner) {
        result.detail = "source sequence not visible to scripting";
        return result;
    }
    var vids = [];
    var bad = 0;
    try {
        for (var t = 0; t < inner.videoTracks.numTracks; t++) {
            var trk = inner.videoTracks[t];
            for (var c = 0; c < trk.clips.numItems; c++) {
                var ic = trk.clips[c];
                var kind = piKind(ic.projectItem);
                vids.push({ clip: ic, kind: kind, track: t });
                if (kind.sequence || kind.merged || kind.multicam || !kind.path) bad++;
            }
        }
    } catch (e) {
        result.detail = "error reading inner sequence: " + e;
        return result;
    }
    result.innerClips = vids;
    if (vids.length === 0) {
        result.cls = "WILL_BREAK";
        result.detail = "no video clips inside the multicam source";
    } else if (bad > 0) {
        result.cls = "WILL_BREAK";
        result.detail = bad + " of " + vids.length +
            " inner clip(s) are nests/merged/pathless -- flatten will drop media";
    } else if (vids.length === 1) {
        result.cls = "RECONSTRUCTABLE";
        result.detail = "single inner source: " + vids[0].kind.path;
    } else {
        result.cls = "REAL_MULTICAM";
        result.detail = vids.length + " angle clips, all with media paths";
    }
    return result;
}

// One unreadable clip must not kill a whole run: errors are collected here
// and reported at the end instead of aborting.
var CLIP_ERRORS = [];

function describeError(e) {
    var where = "";
    try { if (e.line) where = " (toolkit line " + e.line + ")"; } catch (x) {}
    return String(e) + where;
}

function eachVideoClip(seq, fn) {
    for (var t = 0; t < seq.videoTracks.numTracks; t++) {
        var trk = seq.videoTracks[t];
        for (var c = 0; c < trk.clips.numItems; c++) {
            try { fn(trk.clips[c], t, trk, c); }
            catch (e) {
                CLIP_ERRORS.push("V" + (t + 1) + " clip " + (c + 1) + ": " +
                                 describeError(e));
            }
        }
    }
}

function eachAudioClip(seq, fn) {
    for (var t = 0; t < seq.audioTracks.numTracks; t++) {
        var trk = seq.audioTracks[t];
        for (var c = 0; c < trk.clips.numItems; c++) {
            try { fn(trk.clips[c], t, trk, c); }
            catch (e) {
                CLIP_ERRORS.push("A" + (t + 1) + " clip " + (c + 1) + ": " +
                                 describeError(e));
            }
        }
    }
}

// ------------------------------------------------------------------ 1 AUDIT

function runAudit(seq) {
    var fps = seqFps(seq);
    var zero = seqZeroSeconds(seq);
    var lines = [];
    var counts = { willBreak: 0, real: 0, recon: 0, opaque: 0,
                   remap: 0, speed: 0, disabled: 0, offline: 0 };
    // filename -> { path, spots: ["V1 @ TC", ...] } for the offline hit-list
    var offlineMap = {};
    var offlineOrder = [];

    function log(tc, track, name, what) {
        lines.push("[" + tc + "] V" + (track + 1) + "  " + name + "\n    " + what);
    }

    function noteOffline(clip, kind, label, tc, dropMarker) {
        counts.offline++;
        var pname = clip.name;
        try {
            if (clip.projectItem && clip.projectItem.name) pname = clip.projectItem.name;
        } catch (e) {}
        if (!offlineMap[pname]) {
            offlineMap[pname] = { path: kind.path || "(no path recorded)", spots: [] };
            offlineOrder.push(pname);
        }
        // Don't double-mark the linked audio half of an A/V clip at the same TC
        for (var s = 0; s < offlineMap[pname].spots.length; s++) {
            if (offlineMap[pname].spots[s].indexOf("@ " + tc) >= 0) dropMarker = false;
        }
        offlineMap[pname].spots.push(label + " @ " + tc);
        if (dropMarker) {
            addMarker(seq, clip.start.seconds, "OFFLINE: " + pname,
                "Offline media -- track down source file. Last known path: " +
                (kind.path || "none"), MARKER.CYAN);
        }
    }

    eachVideoClip(seq, function (clip, t) {
        var tc = toTC(clip.start.seconds, fps, zero);
        var name = clip.name;
        var kind = piKind(clip.projectItem);

        if (kind.offline) {
            noteOffline(clip, kind, "V" + (t + 1), tc, true);
            log(tc, t, name, "OFFLINE media. Last known path: " +
                (kind.path || "none"));
        }

        if (kind.multicam || kind.sequence) {
            var cls = classifyContainer(clip);
            var label = kind.multicam ? "Multicam" : "Nested sequence";
            if (cls.cls === "WILL_BREAK") {
                counts.willBreak++;
                addMarker(seq, clip.start.seconds, "MC BREAKS: " + name,
                    label + " will lose media on flatten. " + cls.detail, MARKER.RED);
                log(tc, t, name, label + " WILL BREAK on flatten: " + cls.detail);
            } else if (cls.cls === "RECONSTRUCTABLE") {
                counts.recon++;
                addMarker(seq, clip.start.seconds, "MC RECON: " + name,
                    "Run RECONSTRUCT tool. " + cls.detail, MARKER.RED);
                log(tc, t, name, label + " -> single source; RECONSTRUCT tool can fix. " + cls.detail);
            } else if (cls.cls === "REAL_MULTICAM") {
                counts.real++;
                addMarker(seq, clip.start.seconds, "MC OK: " + name,
                    "Real multicam -- flatten via UI. " + cls.detail, MARKER.BLUE);
                log(tc, t, name, label + " looks healthy (" + cls.detail + ") -- flatten via UI.");
            } else {
                counts.opaque++;
                addMarker(seq, clip.start.seconds, "MC OPAQUE: " + name,
                    "Cannot inspect -- check manually. " + cls.detail, MARKER.RED);
                log(tc, t, name, label + " OPAQUE -- check manually: " + cls.detail);
            }
        }

        var sp = clipSpeedInfo(clip);
        if (sp.timeRemap) {
            counts.remap++;
            addMarker(seq, clip.start.seconds, "TIME REMAP: " + name,
                "Speed ramp (" + sp.remapKeys + " keys) -- rebuild in Resolve (Retime Curve).",
                MARKER.ORANGE);
            log(tc, t, name, "Time remap / speed ramp (" + sp.remapKeys + " keyframes).");
        } else if (Math.abs(sp.speed - 1.0) > 0.001 || sp.reversed) {
            counts.speed++;
            addMarker(seq, clip.start.seconds, "SPEED: " + name,
                "Speed " + Math.round(sp.speed * 1000) / 10 + "%" +
                (sp.reversed ? " REVERSED" : ""), MARKER.YELLOW);
            log(tc, t, name, "Constant speed " + Math.round(sp.speed * 1000) / 10 +
                "%" + (sp.reversed ? " (reversed)" : "") + " -- verify after XML import.");
        }

        if (clip.disabled) {
            counts.disabled++;
            addMarker(seq, clip.start.seconds, "DISABLED: " + name,
                "Disabled clip -- run CLEAN tool before exporting XML.", MARKER.PURPLE);
            log(tc, t, name, "Disabled clip.");
        }
    });

    eachAudioClip(seq, function (clip, t) {
        var tc = toTC(clip.start.seconds, fps, zero);
        var kind = piKind(clip.projectItem);
        if (kind.offline) {
            noteOffline(clip, kind, "A" + (t + 1), tc, true);
            lines.push("[" + tc + "] A" + (t + 1) + "  " + clip.name +
                "\n    OFFLINE media. Last known path: " + (kind.path || "none"));
        }
        if (clip.disabled) {
            counts.disabled++;
            lines.push("[" + tc + "] A" + (t + 1) +
                "  " + clip.name + "\n    Disabled clip.");
        }
    });

    // Dedicated offline hit-list: one entry per unique file, every spot listed
    var offlineReportPath = null;
    if (offlineOrder.length) {
        var off = "OFFLINE MEDIA -- " + seq.name + "\n" +
                  offlineOrder.length + " unique file(s), " + counts.offline +
                  " timeline occurrence(s)\n" +
                  "Search these names on your drives / with the editor:\n" +
                  "----------------------------------------------------------------\n";
        var namesOnly = "";
        for (var oi = 0; oi < offlineOrder.length; oi++) {
            var nm = offlineOrder[oi];
            off += "\n" + nm + "\n    last path: " + offlineMap[nm].path + "\n";
            for (var sp = 0; sp < offlineMap[nm].spots.length; sp++) {
                off += "    " + offlineMap[nm].spots[sp] + "\n";
            }
            namesOnly += nm + "\n";
        }
        off += "\n----------------------------------------------------------------\n" +
               "Bare filename list (for copy/paste searching):\n" + namesOnly;
        offlineReportPath = writeTextFile(safeName(seq.name) + "_offline_media.txt", off);
    }

    var head =
        "SLYBURN AUDIT -- " + seq.name + "\n" +
        "Multicams/nests: " + counts.willBreak + " will break, " +
        counts.recon + " reconstructable, " + counts.real + " real, " +
        counts.opaque + " opaque\n" +
        "Time remaps: " + counts.remap + "   Speed changes: " + counts.speed +
        "   Disabled clips: " + counts.disabled + "\n" +
        "Offline media: " + offlineOrder.length + " unique file(s), " +
        counts.offline + " occurrence(s)\n" +
        "Markers dropped on the sequence " +
        "(Red/Blue/Orange/Yellow/Purple, Cyan=offline).\n" +
        "----------------------------------------------------------------\n";
    var path = writeTextFile(safeName(seq.name) + "_audit.txt", head + lines.join("\n"));
    alert(head + "\nReport: " + path +
          (offlineReportPath ? "\nOffline hit-list: " + offlineReportPath : ""));
}

// --------------------------------------------------------------- 2 SNAPSHOT

function runSnapshot(seq) {
    var fps = seqFps(seq);
    var zero = seqZeroSeconds(seq);
    var tag = prompt("Snapshot label (e.g. before_flatten / after_flatten):",
                     "before_flatten");
    if (!tag) return;
    var clips = [];
    function grab(kindLabel) {
        return function (clip, t) {
            var kind = piKind(clip.projectItem);
            clips.push({
                track: kindLabel + (t + 1),
                name: clip.name,
                startSeconds: clip.start.seconds,
                endSeconds: clip.end.seconds,
                inSeconds: clip.inPoint.seconds,
                outSeconds: clip.outPoint.seconds,
                tc: toTC(clip.start.seconds, fps, zero),
                disabled: clip.disabled ? true : false,
                mediaPath: kind.path,
                isMulticam: kind.multicam,
                isNest: kind.sequence,
                isMerged: kind.merged
            });
        };
    }
    eachVideoClip(seq, grab("V"));
    eachAudioClip(seq, grab("A"));
    var doc = { sequence: seq.name, fps: fps, label: tag, clips: clips };
    var path = writeTextFile(safeName(seq.name) + "_snapshot_" + safeName(tag) + ".json",
                             toJson(doc, ""));
    alert("Snapshot (" + clips.length + " clips) -> " + path +
          "\n\nDiff two snapshots with:\n  python3 compare_snapshots.py before.json after.json");
}

// ----------------------------------------------------------------- 3 MOTION

function propByName(comp, wanted) {
    for (var j = 0; j < comp.properties.numItems; j++) {
        var p = comp.properties[j];
        if (String(p.displayName).toLowerCase() === wanted) return p;
    }
    return null;
}

function readProp(p) {
    if (!p) return null;
    var out = { value: null, keyframed: false };
    try { out.value = p.getValue(); } catch (e1) {}
    try { out.keyframed = p.isTimeVarying() ? true : false; } catch (e2) {}
    return out;
}

function runMotionExport(seq) {
    var fps = seqFps(seq);
    var zero = seqZeroSeconds(seq);
    var w = 1920, h = 1080;
    try { w = seq.frameSizeHorizontal; h = seq.frameSizeVertical; } catch (e) {}
    var entries = [];

    eachVideoClip(seq, function (clip, t) {
        var motion = null, opacity = null;
        try {
            for (var i = 0; i < clip.components.numItems; i++) {
                var c = clip.components[i];
                var dn = String(c.displayName || "").toLowerCase();
                if (dn === "motion") motion = c;
                if (dn === "opacity") opacity = c;
            }
        } catch (e1) {}
        if (!motion && !opacity) return;

        var pos = motion ? readProp(propByName(motion, "position")) : null;
        var scale = motion ? readProp(propByName(motion, "scale")) : null;
        var scaleW = motion ? readProp(propByName(motion, "scale width")) : null;
        var rot = motion ? readProp(propByName(motion, "rotation")) : null;
        var opac = opacity ? readProp(propByName(opacity, "opacity")) : null;

        // Only export clips that differ from defaults or are keyframed
        function isDefault(r, defVal) {
            if (!r || r.value === null) return true;
            if (r.keyframed) return false;
            if (r.value instanceof Array) {
                return Math.abs(r.value[0] - 0.5) < 0.0001 &&
                       Math.abs(r.value[1] - 0.5) < 0.0001;
            }
            return Math.abs(r.value - defVal) < 0.0001;
        }
        if (isDefault(pos, null) && isDefault(scale, 100) &&
            isDefault(rot, 0) && isDefault(opac, 100)) return;

        entries.push({
            track: "V" + (t + 1),
            name: clip.name,
            tc: toTC(clip.start.seconds, fps, zero),
            startSeconds: clip.start.seconds,
            frameWidth: w,
            frameHeight: h,
            // Premiere reports Position normalized: 0.5/0.5 = frame center
            positionNorm: pos ? pos.value : null,
            positionKeyframed: pos ? pos.keyframed : false,
            scalePercent: scale ? scale.value : null,
            scaleKeyframed: scale ? scale.keyframed : false,
            scaleWidthPercent: scaleW ? scaleW.value : null,
            rotationDegrees: rot ? rot.value : null,
            rotationKeyframed: rot ? rot.keyframed : false,
            opacityPercent: opac ? opac.value : null,
            opacityKeyframed: opac ? opac.keyframed : false
        });
    });

    var doc = { sequence: seq.name, fps: fps, frameWidth: w, frameHeight: h,
                entries: entries };
    var path = writeTextFile(safeName(seq.name) + "_motion.json", toJson(doc, ""));
    alert("Motion sidecar: " + entries.length + " clip(s) with non-default " +
          "Position/Scale/Rotation/Opacity.\n-> " + path +
          "\n\nAfter importing the XML into Resolve, run:\n" +
          "  python3 apply_motion_sidecar.py " + safeName(seq.name) + "_motion.json");
}

// ------------------------------------------------------------ 4 RECONSTRUCT

function ticksStr(seconds) {
    return String(Math.round(seconds * TICKS_PER_SECOND));
}

function runReconstruct(seq) {
    var fps = seqFps(seq);
    var zero = seqZeroSeconds(seq);
    var half = 0.5 / fps;

    var fixIndex = seq.videoTracks.numTracks - 1;
    var fixTrack = seq.videoTracks[fixIndex];
    if (fixTrack.clips.numItems > 0) {
        alert("The topmost video track (V" + (fixIndex + 1) + ") is not empty.\n" +
              "Add an empty video track at the top (Sequence > Add Tracks) " +
              "to receive the reconstructed clips, then run this again.");
        return;
    }

    var targets = [];
    eachVideoClip(seq, function (clip, t) {
        if (t === fixIndex) return;
        var kind = piKind(clip.projectItem);
        if (!kind.multicam && !kind.sequence) return;
        var cls = classifyContainer(clip);
        if (cls.cls === "RECONSTRUCTABLE") {
            targets.push({ clip: clip, track: t, cls: cls });
        }
    });

    if (!targets.length) {
        alert("No reconstructable multicam/nested clips found.\n" +
              "(Run AUDIT to see how each container was classified.)");
        return;
    }

    var lines = [], ok = 0, fail = 0;
    for (var i = 0; i < targets.length; i++) {
        var tgt = targets[i];
        var clip = tgt.clip;
        var tc = toTC(clip.start.seconds, fps, zero);
        var inner = tgt.cls.innerClips[0];
        var innerClip = inner.clip;
        var innerPi = innerClip.projectItem;

        var sp = clipSpeedInfo(clip);
        if (Math.abs(sp.speed - 1.0) > 0.001 || sp.timeRemap) {
            fail++;
            lines.push("[" + tc + "] " + clip.name +
                "  SKIPPED: outer clip has a speed change/remap -- fix manually.");
            continue;
        }

        // Offset math: outer in/out are times INSIDE the container sequence.
        var outerIn = clip.inPoint.seconds;
        var outerDur = clip.end.seconds - clip.start.seconds;
        var srcIn = innerClip.inPoint.seconds + (outerIn - innerClip.start.seconds);
        var srcOut = srcIn + outerDur;

        if (srcIn < -half) {
            fail++;
            lines.push("[" + tc + "] " + clip.name +
                "  SKIPPED: used range starts before the inner clip does " +
                "(gap inside the container) -- fix manually.");
            continue;
        }

        var placed = false;
        try {
            innerPi.setInPoint(ticksStr(srcIn), 4);
            innerPi.setOutPoint(ticksStr(srcOut), 4);
            placed = fixTrack.overwriteClip(innerPi, clip.start.seconds);
        } catch (e) {
            lines.push("[" + tc + "] " + clip.name + "  ERROR placing clip: " + e);
        }

        // Verify: find the new clip on the FIX track at the same position,
        // same duration (within half a frame).
        var verified = false, why = "clip not found on FIX track";
        if (placed !== false) {
            for (var c = 0; c < fixTrack.clips.numItems; c++) {
                var nc = fixTrack.clips[c];
                if (Math.abs(nc.start.seconds - clip.start.seconds) <= half) {
                    var durDiff = Math.abs(
                        (nc.end.seconds - nc.start.seconds) - outerDur);
                    if (durDiff <= half) { verified = true; }
                    else { why = "duration off by " +
                           Math.round(durDiff * fps) + " frame(s)"; }
                    break;
                }
            }
        }

        if (verified) {
            ok++;
            addMarker(seq, clip.start.seconds, "RECON OK: " + clip.name,
                "Source rebuilt on V" + (fixIndex + 1) + " from " + inner.kind.path,
                MARKER.GREEN);
            lines.push("[" + tc + "] " + clip.name + "  OK -> V" + (fixIndex + 1) +
                "  src " + inner.kind.path +
                "  in " + toTC(srcIn, fps, 0) + " out " + toTC(srcOut, fps, 0));
        } else {
            fail++;
            lines.push("[" + tc + "] " + clip.name + "  VERIFY FAILED: " + why);
        }
    }

    var head =
        "SLYBURN RECONSTRUCT -- " + seq.name + "\n" +
        ok + " reconstructed + verified, " + fail + " need manual attention.\n" +
        "Reconstructed clips are on V" + (fixIndex + 1) + "; the original " +
        "multicam clips were left in place underneath.\n" +
        "Review each green marker, then delete the originals yourself.\n" +
        "NOTE: master-clip in/out points of the source media were changed " +
        "by this process.\n" +
        "NOTE: audio was NOT reconstructed -- handle multicam audio manually.\n" +
        "----------------------------------------------------------------\n";
    var path = writeTextFile(safeName(seq.name) + "_reconstruct.txt",
                             head + lines.join("\n"));
    alert(head + "\nReport: " + path);
}

// ----------------------------------------------------------------- 5 CLEAN

function runClean(seq) {
    var fps = seqFps(seq);
    var zero = seqZeroSeconds(seq);
    var doomed = [];

    function collect(kindLabel) {
        return function (clip, t, trk, idx) {
            if (clip.disabled) {
                doomed.push({ clip: clip, label: kindLabel + (t + 1), trk: trk,
                              idx: idx, name: clip.name,
                              tc: toTC(clip.start.seconds, fps, zero) });
            }
        };
    }
    eachVideoClip(seq, collect("V"));
    eachAudioClip(seq, collect("A"));

    if (!doomed.length) { alert("No disabled clips found. Nothing to clean."); return; }

    var listing = "";
    for (var i = 0; i < doomed.length; i++) {
        listing += "[" + doomed[i].tc + "] " + doomed[i].label + "  " +
                   doomed[i].name + "\n";
    }
    if (!confirm("Delete " + doomed.length + " disabled clip(s)?\n\n" + listing +
                 "\nA log will be written either way.")) {
        writeTextFile(safeName(seq.name) + "_disabled_clips.txt",
                      "DISABLED CLIPS (not deleted):\n" + listing);
        return;
    }

    // Remove back-to-front per track so indices stay valid.
    doomed.sort(function (a, b) { return b.idx - a.idx; });
    var removed = 0, failed = 0;
    for (var j = 0; j < doomed.length; j++) {
        try {
            doomed[j].clip.remove(false, false); // no ripple, no align
            removed++;
        } catch (e) { failed++; }
    }
    var head = "SLYBURN CLEAN -- " + seq.name + "\nDeleted " + removed +
               " disabled clip(s), " + failed + " failed.\n\n";
    var path = writeTextFile(safeName(seq.name) + "_deleted_clips.txt", head + listing);
    alert(head + "Log of every removed clip: " + path);
}

// -------------------------------------------------------------------- menu

function mainMenu() {
    if (typeof app === "undefined" || !app || !app.project) {
        try {
            alert("Not connected to Premiere Pro.\nMake sure Premiere is fully " +
                  "open BEFORE pressing F5, and that the debugger targets " +
                  "'Adobe Premiere Pro'.");
        } catch (e) { $.writeln("Not connected to Premiere Pro."); }
        return;
    }
    var seq = app.project.activeSequence;
    if (!seq) { alert("Open a sequence first (it must be the active sequence)."); return; }

    var dlg = new Window("dialog", "SLYBURN Premiere Toolkit -- " + seq.name);
    dlg.orientation = "column";
    dlg.alignChildren = "fill";
    var choices = [
        "1. AUDIT: mark multicams (will-break vs real), remaps, speed, disabled",
        "2. SNAPSHOT: dump timeline to JSON (run before AND after flatten)",
        "3. MOTION: export Position/Scale/Rotation/Opacity sidecar for Resolve",
        "4. RECONSTRUCT: rebuild broken multicams/nests onto top track + verify",
        "5. CLEAN: delete disabled clips (with log)"
    ];
    var radios = [];
    for (var i = 0; i < choices.length; i++) {
        radios.push(dlg.add("radiobutton", undefined, choices[i]));
    }
    radios[0].value = true;
    var row = dlg.add("group");
    row.alignment = "right";
    var okBtn = row.add("button", undefined, "Run", { name: "ok" });
    row.add("button", undefined, "Cancel", { name: "cancel" });
    okBtn.onClick = function () { dlg.close(1); };
    if (dlg.show() !== 1) return;

    var pick = 0;
    for (var j = 0; j < radios.length; j++) if (radios[j].value) pick = j;
    CLIP_ERRORS.length = 0;
    try {
        if (pick === 0) runAudit(seq);
        else if (pick === 1) runSnapshot(seq);
        else if (pick === 2) runMotionExport(seq);
        else if (pick === 3) runReconstruct(seq);
        else if (pick === 4) runClean(seq);
    } catch (e) {
        alert("SLYBURN toolkit hit an error it couldn't recover from:\n\n" +
              describeError(e) +
              "\n\nSend this message (and the line number, if shown) to Claude " +
              "to get a fix.");
    }
    if (CLIP_ERRORS.length) {
        var errPath = writeTextFile(safeName(seq.name) + "_clip_errors.txt",
                                    CLIP_ERRORS.join("\n"));
        alert(CLIP_ERRORS.length + " clip(s) could not be fully read and were " +
              "skipped (everything else completed).\nDetails: " + errPath +
              "\nSend that file to Claude to widen compatibility.");
    }
}

mainMenu();
