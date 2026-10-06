import os
import sqlite3
import unittest

from helpers import TempState, stickies


class StoreTest(TempState):
    def setUp(self):
        super().setUp()
        self.s = stickies.Store()

    def tearDown(self):
        self.s.close()
        super().tearDown()

    def test_state_location(self):
        self.assertEqual(self.s.path, os.path.join(self.state, "stickies.db"))
        self.s.add("x")
        self.assertTrue(os.path.exists(os.path.join(self.state, "stickies.log")))

    def test_add_defaults_and_columns(self):
        n = self.s.add("hello")
        self.assertEqual(tuple(n), stickies.COLUMNS)
        self.assertEqual(n["color"], "yellow")
        self.assertIs(n["pinned"], False)
        self.assertIsNone(n["archived_at"])
        self.assertIsNone(n["workspace"])
        self.assertEqual((n["w"], n["h"]), (240, 200))
        self.assertTrue(n["created_at"].endswith("Z"))
        m = self.s.add("second")
        self.assertGreater(m["z"], n["z"])
        self.assertNotEqual((m["x"], m["y"]), (n["x"], n["y"]))

    def test_colors(self):
        n = self.s.add("c", color="Pink")
        self.assertEqual(n["color"], "pink")
        self.assertEqual(self.s.color(n["id"], "#AABBCC")["color"], "#aabbcc")
        with self.assertRaises(stickies.StickiesError):
            self.s.color(n["id"], "chartreuse")

    def test_edit_and_append(self):
        n = self.s.add("line one")
        self.assertEqual(self.s.edit(n["id"], body="new")["body"], "new")
        self.assertEqual(self.s.edit(n["id"], append="more")["body"], "new\nmore")
        with self.assertRaises(stickies.StickiesError):
            self.s.edit(999, body="x")

    def test_move_and_raise(self):
        a = self.s.add("a")
        b = self.s.add("b")
        m = self.s.move(a["id"], x=10, y=20, w=300, h=150, workspace=3, monitor="eDP-1")
        self.assertEqual((m["x"], m["y"], m["w"], m["h"], m["workspace"], m["monitor"]),
                         (10, 20, 300, 150, 3, "eDP-1"))
        r = self.s.move(a["id"], raise_=True)
        self.assertGreater(r["z"], self.s.get(b["id"])["z"])
        with self.assertRaises(stickies.StickiesError):
            self.s.move(a["id"], w=0)

    def test_pin_ordering(self):
        a = self.s.add("a")
        self.s.add("b")
        self.s.pin(a["id"])
        self.assertEqual(self.s.list()[0]["id"], a["id"])
        self.assertEqual([n["id"] for n in self.s.list(pinned=True)], [a["id"]])

    def test_archive_restore_purge(self):
        a = self.s.add("keep me findable")
        self.s.archive(a["id"])
        self.assertEqual(self.s.list(), [])
        self.assertEqual(len(self.s.list(archived=True)), 1)
        self.assertEqual(self.s.search("findable"), [])
        self.assertEqual(len(self.s.search("findable", archived=True)), 1)
        self.s.restore(a["id"])
        self.assertEqual(len(self.s.search("findable")), 1)
        with self.assertRaises(stickies.StickiesError):
            self.s.purge(a["id"])  # not archived
        self.s.archive(a["id"])
        self.s.purge(a["id"])
        with self.assertRaises(stickies.StickiesError):
            self.s.get(a["id"])
        self.assertEqual(self.s.search("findable", archived=True), [])

    def test_filters(self):
        self.s.add("a", workspace=1, color="blue")
        self.s.add("b", workspace=2)
        self.assertEqual([n["body"] for n in self.s.list(workspace=1)], ["a"])
        self.assertEqual([n["body"] for n in self.s.list(color="blue")], ["a"])
        self.assertEqual(len(self.s.list(limit=1)), 1)

    def test_schema_survives_reopen(self):
        self.s.add("persist")
        self.s.close()
        self.s = stickies.Store()
        self.assertEqual(self.s.search("pers")[0]["body"], "persist")


class SearchTest(TempState):
    def setUp(self):
        super().setUp()
        self.s = stickies.Store()
        self.fees = self.s.add("Cardmarket fees went up to 5% for [OP-05] singles")["id"]
        self.cafe = self.s.add("Koffie in het café met Jan, vrijdag")["id"]
        self.btw = self.s.add("BTW kwartaalaangifte Q3 deadline 31 oktober")["id"]
        self.both = self.s.add("fees fees fees: cardmarket fees again")["id"]

    def tearDown(self):
        self.s.close()
        super().tearDown()

    def ids(self, q, **kw):
        return [h["id"] for h in self.s.search(q, **kw)]

    def test_prefix_as_you_type(self):
        for partial in ("c", "ca", "car", "card", "cardm", "cardmarket"):
            self.assertIn(self.fees, self.ids(partial), partial)

    def test_all_words_required(self):
        self.assertEqual(self.ids("kwartaal okt"), [self.btw])
        self.assertEqual(self.ids("kwartaal cardmarket"), [])

    def test_diacritics_folded(self):
        self.assertEqual(self.ids("cafe"), [self.cafe])
        self.assertEqual(self.ids("café"), [self.cafe])

    def test_bm25_ranking(self):
        hits = self.s.search("fees")
        self.assertEqual(hits[0]["id"], self.both)
        self.assertGreater(hits[0]["score"], hits[1]["score"])

    def test_fts_syntax_is_escaped(self):
        for q in ('"', "fees AND", "OR", "NEAR(", "-fees", "fees*", "col:fees", "[OP-05]", "^", "(("):
            self.s.search(q)  # must not raise
        self.assertEqual(self.ids("[OP-05]"), [self.fees])
        self.assertEqual(self.s.search(""), [])
        self.assertEqual(self.s.search("  !!  "), [])

    def test_snippet_highlights(self):
        h = self.s.search("cardm")[0]
        self.assertNotIn("\x01", h["snippet"])
        for a, b in h["highlights"]:
            self.assertTrue(h["snippet"][a:b].lower().startswith("cardm"))
        self.assertTrue(h["highlights"])

    def test_index_tracks_edits_and_deletes(self):
        self.s.edit(self.cafe, body="Thee bij oma")
        self.assertEqual(self.ids("cafe"), [])
        self.assertEqual(self.ids("oma"), [self.cafe])
        self.s.archive(self.cafe)
        self.s.purge(self.cafe)
        self.assertEqual(self.ids("oma", archived=True), [])
        # Index stays consistent with the content table.
        self.s.db.execute("INSERT INTO notes_fts(notes_fts) VALUES('integrity-check')")

    def test_non_body_updates_dont_touch_index(self):
        self.s.move(self.fees, x=5)
        self.s.pin(self.fees)
        self.assertIn(self.fees, self.ids("cardmarket"))
        self.s.db.execute("INSERT INTO notes_fts(notes_fts) VALUES('integrity-check')")


class ChangeFeedTest(TempState):
    def test_coalesced_events(self):
        s = stickies.Store()
        seq = s.last_seq()
        a = s.add("a")
        s.move(a["id"], x=1)
        s.move(a["id"], x=2)
        b = s.add("b")
        s.archive(b["id"])
        seq, evs = s.changes_since(seq)
        self.assertEqual([(e["id"], e["kind"]) for e in evs], [(a["id"], "update"), (b["id"], "archive")])
        self.assertEqual(evs[0]["note"]["x"], 2)
        s.purge(b["id"])
        seq, evs = s.changes_since(seq)
        self.assertEqual(evs, [{"event": "changed", "seq": seq, "kind": "purge", "id": b["id"], "note": None}])
        self.assertEqual(s.changes_since(seq), (seq, []))
        s.close()

    def test_other_connection_writes_visible(self):
        s = stickies.Store()
        seq = s.last_seq()
        other = sqlite3.connect(s.path)
        other.execute("INSERT INTO notes(body, created_at, updated_at) VALUES ('ext', 't', 't')")
        other.commit()
        other.close()
        _, evs = s.changes_since(seq)
        self.assertEqual(evs[0]["kind"], "add")
        self.assertEqual(evs[0]["note"]["body"], "ext")
        s.close()


if __name__ == "__main__":
    unittest.main()
