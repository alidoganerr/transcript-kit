#!/usr/bin/env python3
"""Turn meeting and chat transcripts into one JSON shape, and count who talked how much.

    python3 transcript_kit.py convert call.vtt -o call.json
    python3 transcript_kit.py stats call.vtt
    python3 transcript_kit.py report chat.txt -o report.html

Reads WebVTT (Zoom, Teams), SRT, WhatsApp exports and plain "Name: text" files.
"""

import argparse
import html
import json
import os
import re
import sys

TIME = r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
CUE = re.compile(r"^\s*%s\s*-->\s*%s" % (TIME, TIME))
VOICE = re.compile(r"^<v\s+([^>]+)>(.*?)(?:</v>)?$")
NAME_LINE = re.compile(r"^([^:\[\]<>]{1,60}?):\s+(.+)$")
STAMPED = re.compile(r"^\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?\s+([^:]{1,60}?):\s+(.+)$")
TAG = re.compile(r"</?[a-z][^>]*>", re.I)

# WhatsApp: "12.11.2026 14:03 - Ayşe: ..." (Android) or "[12.11.2026 14:03:21] Ayşe: ..." (iPhone).
# Dates come with dots, slashes or dashes and sometimes a comma, depending on phone language.
WA_LINE = re.compile(
    r"^‎?\[?(\d{1,4}[./-]\d{1,2}[./-]\d{2,4}),?\s+(\d{1,2}:\d{2}(?::\d{2})?)(?:\s?[APap][Mm])?\]?\s*(?:-\s*)?(.*)$")
WA_MEDIA = re.compile(r"^‎?<?(medya dahil edilmedi|media omitted|görüntü dahil edilmedi|"
                      r"image omitted|video omitted|audio omitted|sticker omitted)>?$", re.I)

WORD = re.compile(r"[\w'’-]+", re.UNICODE)


class TranscriptError(Exception):
    pass


def _seconds(h, m, s, frac):
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(frac.ljust(3, "0")) / 1000


def _clock(text):
    parts = [int(p) for p in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def guess_format(text, filename=""):
    ext = os.path.splitext(filename)[1].lower()
    head = text.lstrip("﻿").lstrip()
    if head.startswith("WEBVTT") or ext == ".vtt":
        return "vtt"
    if ext == ".srt" or re.match(r"^\d+\s*\n\s*\d{1,2}:\d{2}:\d{2},\d{3}\s*-->", head):
        return "srt"
    first = [l for l in head.splitlines()[:5] if l.strip()]
    if first and sum(1 for l in first if WA_LINE.match(l)) >= max(1, len(first) // 2):
        return "whatsapp"
    return "text"


def _split_speaker(line):
    m = VOICE.match(line)
    if m:
        return m.group(1).strip(), TAG.sub("", m.group(2)).strip()
    m = NAME_LINE.match(line)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, TAG.sub("", line).strip()


def parse_cues(text):
    """VTT and SRT. A cue without a speaker keeps the previous cue's speaker."""
    out = []
    speaker = None
    block = []
    for line in text.splitlines() + [""]:
        if line.strip():
            block.append(line)
            continue
        if not block:
            continue
        lines, block = block, []
        idx = next((i for i, l in enumerate(lines) if CUE.match(l)), None)
        if idx is None:
            continue  # WEBVTT header, NOTE, STYLE, or a stray number
        m = CUE.match(lines[idx])
        start, end = _seconds(*m.groups()[:4]), _seconds(*m.groups()[4:])
        texts = []
        for raw in lines[idx + 1:]:
            who, words = _split_speaker(raw.strip())
            if who:
                speaker = who
            if words:
                texts.append(words)
        if texts:
            out.append({"speaker": speaker or "Unknown", "start": start, "end": end, "text": " ".join(texts)})
    return out


def parse_whatsapp(text):
    out = []
    for line in text.splitlines():
        m = WA_LINE.match(line)
        if not m:
            # a message that runs over several lines
            if out and line.strip():
                out[-1]["text"] += "\n" + line.strip()
            continue
        rest = m.group(3)
        who, _, body = rest.partition(": ")
        if not body or len(who) > 60:
            continue  # "Messages are end-to-end encrypted", "X added Y", ...
        body = body.strip().lstrip("‎")
        if WA_MEDIA.match(body):
            continue
        out.append({"speaker": who.strip().lstrip("‎"), "start": None, "end": None,
                    "date": m.group(1), "time": m.group(2), "text": body})
    return out


def parse_text(text):
    """'Name: text' lines, optionally with a [mm:ss] or hh:mm:ss stamp in front."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        m = STAMPED.match(s)
        if m:
            t = _clock(m.group(1))
            out.append({"speaker": m.group(2).strip(), "start": t, "end": None, "text": m.group(3).strip()})
            continue
        m = NAME_LINE.match(s)
        if m and not m.group(1).strip().startswith("http"):
            out.append({"speaker": m.group(1).strip(), "start": None, "end": None, "text": m.group(2).strip()})
        elif out:
            out[-1]["text"] += " " + s
    return out


PARSERS = {"vtt": parse_cues, "srt": parse_cues, "whatsapp": parse_whatsapp, "text": parse_text}


def merge_turns(utterances):
    """Back-to-back lines from the same person become one turn."""
    turns = []
    for u in utterances:
        if turns and turns[-1]["speaker"] == u["speaker"]:
            t = turns[-1]
            t["text"] += " " + u["text"]
            if u.get("end") is not None:
                t["end"] = u["end"]
            continue
        t = {"speaker": u["speaker"], "start": u.get("start"), "end": u.get("end"), "text": u["text"]}
        if "time" in u:  # chat exports have a clock time instead of an offset
            t["date"], t["time"] = u["date"], u["time"]
        turns.append(t)
    return turns


def load(path, fmt=None, rename=None):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read()
    except UnicodeDecodeError:
        raise TranscriptError("%s is not UTF-8 text" % path)
    except OSError as exc:
        raise TranscriptError("can't read %s: %s" % (path, exc.strerror or exc))
    fmt = fmt or guess_format(text, path)
    if fmt not in PARSERS:
        raise TranscriptError("unknown format %r (vtt, srt, whatsapp, text)" % fmt)
    utterances = PARSERS[fmt](text)
    if not utterances:
        raise TranscriptError("no speaker lines found in %s (read as %s)" % (path, fmt))
    for u in utterances:
        u["speaker"] = (rename or {}).get(u["speaker"], u["speaker"])
    return {"source": os.path.basename(path), "format": fmt, "turns": merge_turns(utterances)}


def words(text):
    return len(WORD.findall(text))


def questions(text):
    # only "?" counts; chat messages often leave it out, so WhatsApp numbers run low
    return len(re.findall(r"\?+", text))


def stats(doc):
    turns = doc["turns"]
    people = {}
    for t in turns:
        p = people.setdefault(t["speaker"], {"turns": 0, "words": 0, "questions": 0, "longest": 0, "seconds": 0.0})
        w = words(t["text"])
        p["turns"] += 1
        p["words"] += w
        p["questions"] += questions(t["text"])
        p["longest"] = max(p["longest"], w)
        if t.get("start") is not None and t.get("end") is not None:
            p["seconds"] += max(0.0, t["end"] - t["start"])
    total = sum(p["words"] for p in people.values()) or 1
    for p in people.values():
        p["share"] = round(100 * p["words"] / total, 1)
        p["avg_words"] = round(p["words"] / p["turns"], 1)
    longest = max(turns, key=lambda t: words(t["text"]))
    timed = [t for t in turns if t.get("start") is not None]
    duration = None
    if timed:
        last = max((t.get("end") or t["start"]) for t in timed)
        duration = round(last - min(t["start"] for t in timed), 1)
    return {
        "turns": len(turns),
        "words": sum(p["words"] for p in people.values()),
        "duration_seconds": duration,
        "speakers": dict(sorted(people.items(), key=lambda kv: -kv[1]["words"])),
        "longest_turn": {"speaker": longest["speaker"], "words": words(longest["text"]),
                         "start": longest.get("start"), "text": longest["text"][:200]},
    }


def _mmss(sec):
    if sec is None:
        return "-"
    sec = int(round(sec))
    return "%d:%02d" % divmod(sec, 60) if sec < 3600 else "%d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


def stats_table(doc, st):
    rows = ["%s (%s, %d turns, %d words%s)" % (doc["source"], doc["format"], st["turns"], st["words"],
                                               ", " + _mmss(st["duration_seconds"]) if st["duration_seconds"] else "")]
    rows.append("")
    rows.append("%-24s %6s %6s %6s %8s %8s %4s" % ("speaker", "share", "turns", "words", "avg/turn", "longest", "?"))
    for name, p in st["speakers"].items():
        rows.append("%-24s %5.1f%% %6d %6d %8.1f %8d %4d" % (
            name[:24], p["share"], p["turns"], p["words"], p["avg_words"], p["longest"], p["questions"]))
    lt = st["longest_turn"]
    rows.append("")
    rows.append("Longest turn: %s, %d words%s" % (lt["speaker"], lt["words"],
                                                  " at " + _mmss(lt["start"]) if lt["start"] is not None else ""))
    return "\n".join(rows)


COLORS = ["#2f5d8a", "#d29a00", "#4f8a5b", "#8a4f7d", "#b03a2e", "#5b5f66"]

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body{{font:15px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif;color:#1d1d1f;background:#fff;max-width:860px;margin:2rem auto;padding:0 1rem}}
h1{{font-size:1.3rem;margin:0 0 .2rem}} .meta{{color:#666;margin-bottom:1.5rem}}
table{{border-collapse:collapse;width:100%;margin:1rem 0}} th,td{{text-align:left;padding:.35rem .5rem;border-bottom:1px solid #e4e4e0}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}}
.bar{{height:.7rem;border-radius:2px}} .swatch{{display:inline-block;width:.7rem;height:.7rem;border-radius:2px;margin-right:.4rem;vertical-align:-1px}}
.timeline{{display:flex;height:1.2rem;border-radius:3px;overflow:hidden;margin:.5rem 0 1.5rem;background:#eee}}
.turns p{{margin:.3rem 0}} .turns b{{font-weight:600}} .t{{color:#888;font-size:.85em;margin-right:.4rem}}
</style></head><body>
<h1>{title}</h1>
<div class="meta">{meta}</div>
<table><tr><th>Speaker</th><th>Share of words</th><th></th><th class="n">Turns</th><th class="n">Words</th><th class="n">Avg / turn</th><th class="n">Longest</th><th class="n">Questions</th></tr>
{rows}
</table>
{timeline}
<h2 style="font-size:1rem">Longest turn</h2>
<p>{longest}</p>
<details><summary>Full transcript ({nturns} turns)</summary><div class="turns">{turns}</div></details>
</body></html>
"""


def html_report(doc, st):
    names = list(st["speakers"])
    color = {n: COLORS[i % len(COLORS)] for i, n in enumerate(names)}
    e = html.escape
    rows = []
    for n, p in st["speakers"].items():
        rows.append('<tr><td><span class="swatch" style="background:%s"></span>%s</td>'
                    '<td style="width:30%%"><div class="bar" style="width:%.1f%%;background:%s"></div></td>'
                    '<td class="n">%.1f%%</td><td class="n">%d</td><td class="n">%d</td><td class="n">%.1f</td>'
                    '<td class="n">%d</td><td class="n">%d</td></tr>'
                    % (color[n], e(n), p["share"], color[n], p["share"], p["turns"], p["words"],
                       p["avg_words"], p["longest"], p["questions"]))
    timeline = ""
    if st["duration_seconds"]:
        segs = []
        t0 = min(t["start"] for t in doc["turns"] if t.get("start") is not None)
        for t in doc["turns"]:
            if t.get("start") is None or t.get("end") is None:
                continue
            w = 100 * (t["end"] - t["start"]) / st["duration_seconds"]
            left = 100 * (t["start"] - t0) / st["duration_seconds"]
            segs.append('<div title="%s %s" style="position:absolute;left:%.2f%%;width:%.2f%%;height:100%%;background:%s"></div>'
                        % (e(t["speaker"]), _mmss(t["start"]), left, max(w, 0.2), color[t["speaker"]]))
        timeline = ('<h2 style="font-size:1rem">Who spoke when</h2>'
                    '<div class="timeline" style="position:relative">%s</div>' % "".join(segs))
    turns = "".join('<p><span class="t">%s</span><b>%s:</b> %s</p>'
                    % (_mmss(t.get("start")) if t.get("start") is not None else e(t.get("time", "")),
                       e(t["speaker"]), e(t["text"]).replace("\n", "<br>"))
                    for t in doc["turns"])
    lt = st["longest_turn"]
    meta = "%s, %d turns, %d words" % (e(doc["format"]), st["turns"], st["words"])
    if st["duration_seconds"]:
        meta += ", " + _mmss(st["duration_seconds"])
    return PAGE.format(title=e(doc["source"]), meta=meta, rows="\n".join(rows), timeline=timeline,
                       longest="<b>%s</b> (%d words): %s" % (e(lt["speaker"]), lt["words"], e(lt["text"])),
                       nturns=len(doc["turns"]), turns=turns)


def _rename(pairs):
    out = {}
    for p in pairs or []:
        old, sep, new = p.partition("=")
        if not sep or not old.strip() or not new.strip():
            raise TranscriptError("--rename wants OLD=NEW, got %r" % p)
        out[old.strip()] = new.strip()
    return out


def _write(path, text):
    if path in (None, "-"):
        sys.stdout.write(text)
        return
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="transcript_kit", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, what in (("convert", "write the transcript as JSON"),
                       ("stats", "print talk share, turns and questions per speaker"),
                       ("report", "write a one-page HTML report")):
        p = sub.add_parser(name, help=what)
        p.add_argument("file")
        p.add_argument("--format", choices=sorted(PARSERS), help="skip detection")
        p.add_argument("--rename", action="append", metavar="OLD=NEW", help="rename a speaker, repeatable")
        if name != "stats":
            p.add_argument("-o", "--out", help="output file (default: stdout)")
        else:
            p.add_argument("--json", action="store_true", help="print the numbers as JSON")
    args = ap.parse_args(argv)
    try:
        doc = load(args.file, args.format, _rename(args.rename))
        if args.cmd == "convert":
            _write(args.out, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        elif args.cmd == "stats":
            st = stats(doc)
            print(json.dumps(st, ensure_ascii=False, indent=1) if args.json else stats_table(doc, st))
        else:
            _write(args.out, html_report(doc, stats(doc)))
    except TranscriptError as exc:
        print("transcript_kit: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
