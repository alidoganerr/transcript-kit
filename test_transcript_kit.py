import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

import transcript_kit as tk

HERE = os.path.dirname(os.path.abspath(__file__))

ZOOM_VTT = """WEBVTT

1
00:00:01.000 --> 00:00:04.500
Ana Silva: Can everyone hear me?

2
00:00:05.000 --> 00:00:07.000
Ben Ito: Yes, loud and clear.

3
00:00:07.200 --> 00:00:09.000
Ben Ito: Shall we start?
"""

SRT = """1
00:00:00,500 --> 00:00:02,000
Ana: Hi there.

2
00:00:02,500 --> 00:00:05,000
Ben: Hello. How was the trip?
"""

WA_IOS = """[18.11.2026 21:03:10] Ece: Yarın toplantı var mı?
[18.11.2026 21:04:02] Kaan: Var, 10'da
‎[18.11.2026 21:04:30] Kaan: ‎image omitted
[18.11.2026 21:05:11] Ece: Tamam
ikinci satır
"""

WA_ANDROID_EN = """11/18/26, 9:03 PM - Messages and calls are end-to-end encrypted. No one outside of this chat can read them.
11/18/26, 9:04 PM - Ece: Are we still on for tomorrow?
11/18/26, 9:05 PM - Kaan: Yes
"""

PLAIN = """[00:01] Ana: First point.
[00:15] Ben: Agreed.
and one more thing
"""


def write(folder, name, text):
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class ParseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def load(self, name, text, **kw):
        return tk.load(write(self.tmp, name, text), **kw)

    def test_zoom_vtt(self):
        doc = self.load("a.vtt", ZOOM_VTT)
        self.assertEqual(doc["format"], "vtt")
        self.assertEqual([t["speaker"] for t in doc["turns"]], ["Ana Silva", "Ben Ito"])
        ben = doc["turns"][1]
        self.assertEqual(ben["text"], "Yes, loud and clear. Shall we start?")
        self.assertEqual((ben["start"], ben["end"]), (5.0, 9.0))

    def test_teams_voice_tags(self):
        text = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Ana Silva>Hello</v>\n\n" \
               "00:00:02.500 --> 00:00:03.000\n<v Ben Ito>Hi</v>\n"
        doc = self.load("teams.vtt", text)
        self.assertEqual([(t["speaker"], t["text"]) for t in doc["turns"]], [("Ana Silva", "Hello"), ("Ben Ito", "Hi")])

    def test_cue_without_speaker_keeps_previous(self):
        text = "WEBVTT\n\n00:01.000 --> 00:02.000\nAna: one\n\n00:02.000 --> 00:03.000\ntwo\n"
        doc = self.load("x.vtt", text)
        self.assertEqual(doc["turns"], [{"speaker": "Ana", "start": 1.0, "end": 3.0, "text": "one two"}])

    def test_srt(self):
        doc = self.load("a.srt", SRT)
        self.assertEqual(doc["format"], "srt")
        self.assertEqual(doc["turns"][1]["start"], 2.5)

    def test_whatsapp_iphone(self):
        doc = self.load("chat.txt", WA_IOS)
        self.assertEqual(doc["format"], "whatsapp")
        self.assertEqual([t["speaker"] for t in doc["turns"]], ["Ece", "Kaan", "Ece"])
        self.assertEqual(doc["turns"][2]["text"], "Tamam\nikinci satır")
        self.assertEqual(doc["turns"][0]["time"], "21:03:10")

    def test_whatsapp_android_skips_system_lines(self):
        doc = self.load("chat.txt", WA_ANDROID_EN)
        self.assertEqual([t["speaker"] for t in doc["turns"]], ["Ece", "Kaan"])

    def test_plain_text_with_stamps(self):
        doc = self.load("notes.txt", PLAIN)
        self.assertEqual(doc["format"], "text")
        self.assertEqual(doc["turns"][1], {"speaker": "Ben", "start": 15, "end": None,
                                           "text": "Agreed. and one more thing"})

    def test_rename(self):
        doc = self.load("a.srt", SRT, rename={"Ana": "Ana Silva"})
        self.assertEqual(doc["turns"][0]["speaker"], "Ana Silva")

    def test_nothing_to_read(self):
        with self.assertRaises(tk.TranscriptError):
            self.load("empty.txt", "just a line without a speaker\n")


class StatsTests(unittest.TestCase):
    def test_numbers(self):
        doc = {"source": "s", "format": "vtt", "turns": [
            {"speaker": "A", "start": 0.0, "end": 4.0, "text": "one two three four?"},
            {"speaker": "B", "start": 4.0, "end": 6.0, "text": "five six"},
            {"speaker": "A", "start": 6.0, "end": 10.0, "text": "seven eight"},
        ]}
        st = tk.stats(doc)
        a = st["speakers"]["A"]
        self.assertEqual((a["turns"], a["words"], a["questions"], a["longest"]), (2, 6, 1, 4))
        self.assertEqual(a["share"], 75.0)
        self.assertEqual(a["seconds"], 8.0)
        self.assertEqual(st["duration_seconds"], 10.0)
        self.assertEqual(st["longest_turn"]["speaker"], "A")
        self.assertEqual(list(st["speakers"]), ["A", "B"])

    def test_turkish_words(self):
        self.assertEqual(tk.words("Yarın İstanbul'a gidiyoruz, gelir misin?"), 5)

    def test_html_is_escaped(self):
        doc = {"source": "<b>x</b>", "format": "text", "turns": [
            {"speaker": "<script>", "start": None, "end": None, "text": "a & b"}]}
        page = tk.html_report(doc, tk.stats(doc))
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("a &amp; b", page)


class CliTests(unittest.TestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = tk.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_examples(self):
        vtt = os.path.join(HERE, "examples", "team-sync.vtt")
        wa = os.path.join(HERE, "examples", "hafta-sonu.txt")
        code, out, _ = self.run_main("stats", vtt)
        self.assertEqual(code, 0)
        self.assertIn("Deniz Arslan", out)
        code, out, _ = self.run_main("convert", wa)
        doc = json.loads(out)
        self.assertEqual(doc["format"], "whatsapp")
        self.assertNotIn("Mesajlar ve aramalar", out)

    def test_report_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "r.html")
            code, _, _ = self.run_main("report", os.path.join(HERE, "examples", "team-sync.vtt"), "-o", out)
            self.assertEqual(code, 0)
            with open(out, encoding="utf-8") as fh:
                self.assertIn("Who spoke when", fh.read())

    def test_bad_rename_and_missing_file(self):
        code, _, err = self.run_main("stats", "nope.vtt")
        self.assertEqual(code, 1)
        self.assertIn("can't read", err)
        code, _, err = self.run_main("stats", os.path.join(HERE, "examples", "team-sync.vtt"), "--rename", "x")
        self.assertEqual(code, 1)
        self.assertIn("OLD=NEW", err)


if __name__ == "__main__":
    unittest.main()
