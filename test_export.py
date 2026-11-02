"""Tests on a fake Discord server (no network). Run: python3 -m unittest test_export.py"""
import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import export

GUILD = "1"


def snowflake(ms_since_2015):
    return str(ms_since_2015 << 22)


def msg(mid, author, ts, mtype=0, reply_to=None, mentions=(), bot=False, thread=None):
    m = {"id": mid, "timestamp": ts, "type": mtype, "author": {"id": author, "bot": bot},
         "content": "", "mentions": [{"id": u} for u in mentions],
         "reactions": [{"count": 2}], "attachments": []}
    if reply_to:
        m["message_reference"] = {"message_id": reply_to}
    if thread:
        m["thread"] = {"id": thread}
    return m


# message ids are real snowflakes so that reply_delay can be computed from them
T0 = 1_700_000_000_000 - export.DISCORD_EPOCH_MS  # 2023-11-14T22:13:20Z
M1, M2, M3, M4 = (snowflake(T0 + s * 1000) for s in (0, 600, 7200, 8700))

FAKE = {
    f"/guilds/{GUILD}/channels": [
        {"id": "10", "name": "Accueil", "type": 4},
        {"id": "20", "name": "bienvenue", "type": 0, "parent_id": "10"},
        {"id": "21", "name": "vocal", "type": 2, "parent_id": "10"},
        {"id": "30", "name": "Équipe", "type": 4},
        {"id": "31", "name": "modo", "type": 0, "parent_id": "30"},
    ],
    "/channels/20/threads/archived/public?limit=100": {
        "threads": [{"id": "40", "name": "Accueil de Jean Dupont", "type": 11, "parent_id": "20",
                     "thread_metadata": {"archive_timestamp": "2023-11-15T00:00:00+00:00"}}],
        "has_more": False},
    f"/guilds/{GUILD}/threads/active": {"threads": []},
    "/channels/20/messages?limit=100": [
        msg(M4, "102", "2023-11-15T00:38:20+00:00", mtype=19, reply_to=M3),
        msg(M3, "101", "2023-11-15T00:13:20+00:00", thread="40"),
        msg(M2, "102", "2023-11-14T22:23:20.123000+00:00", mtype=19, reply_to=M1, mentions=["101"]),
        msg(M1, "101", "2023-11-14T22:13:20+00:00"),
        msg("5", "999", "2023-11-14T22:00:00+00:00"),  # excluded member
    ],
    "/channels/40/messages?limit=100": [msg("6", "103", "2023-11-15T01:00:00+00:00")],
    "/channels/31/messages?limit=100": [msg("7", "101", "2023-11-15T01:00:00+00:00")],
    f"/guilds/{GUILD}/roles": [
        {"id": "r1", "name": "bénévole"}, {"id": "r2", "name": "Président"},
        {"id": "r3", "name": "Groupe Lyon"}, {"id": "r4", "name": "BotRole", "managed": True}],
    f"/guilds/{GUILD}/members?limit=1000&after=0": [
        {"user": {"id": "101"}, "joined_at": "2023-11-14T21:59:59.5+00:00", "roles": ["r1", "r2", "r4"]},
        {"user": {"id": "102"}, "joined_at": "2023-01-02T10:00:00+00:00", "roles": ["r1", "r3"]},
        {"user": {"id": "103"}, "joined_at": "2023-01-03T10:00:00+00:00", "roles": ["r1"]},
        {"user": {"id": "999"}, "joined_at": "2023-01-03T10:00:00+00:00", "roles": ["r1"]},
        {"user": {"id": "500", "bot": True}, "joined_at": "2023-01-03T10:00:00+00:00", "roles": ["r4"]},
    ],
}

ENV = {"DISCORD_BOT_TOKEN": "x", "GUILD_ID": GUILD, "PSEUDO_SALT": "salt",
       "EXCLUDED_USER_IDS": "999", "EXCLUDED_CHANNELS": "30",
       "ROLE_MIN_MEMBERS": "2", "ROLES_ALWAYS_KEEP": "Groupe Lyon"}


def fake_get(path, token):
    calls.append(path)
    if path in FAKE:
        return FAKE[path]
    if path.startswith("/channels/") and "/threads/archived" not in path:
        return []  # later pages / incremental runs: nothing new
    return None


calls = []


def read_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = dict(ENV, CACHE_DIR=f"{self.tmp.name}/cache", OUT_DIR=f"{self.tmp.name}/out")
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        calls.clear()
        with mock.patch.object(export, "get", fake_get):
            export.run("x", GUILD)
        self.out = Path(self.tmp.name) / "out"
        self.messages = read_csv(self.out / "messages.csv")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_no_text_and_no_discord_message_ids(self):
        self.assertNotIn("content", self.messages[0])
        self.assertNotIn("attachments", self.messages[0])
        fields = {v for p in self.out.iterdir() for row in csv.reader(p.open()) for v in row}
        for real_id in (M1, M2, M3, M4, "40", "101", "102"):
            self.assertNotIn(real_id, fields)

    def test_excluded_member_and_channels(self):
        authors = {m["author"] for m in self.messages}
        self.assertNotIn(export.pseudo("999"), authors)
        self.assertEqual(len(self.messages), 5)  # 4 in #bienvenue + 1 in thread, #modo excluded
        self.assertFalse(any("/channels/31/" in c for c in calls))
        channels = read_csv(self.out / "channels.csv")
        self.assertEqual({c["name"] for c in channels}, {"bienvenue", ""})  # no #modo, no empty #vocal

    def test_no_archived_threads_call_on_voice(self):
        self.assertFalse(any(c.startswith("/channels/21/threads") for c in calls))

    def test_thread_name_dropped_and_linked(self):
        channels = read_csv(self.out / "channels.csv")
        thread = next(c for c in channels if c["type"] == "thread")
        self.assertEqual(thread["name"], "")
        self.assertEqual(thread["parent_channel"], "20")
        self.assertEqual(thread["category"], "Accueil")
        started = next(m for m in self.messages if m["thread_started"])
        self.assertEqual(started["thread_started"], thread["channel"])

    def test_hours_rank_reply_delay(self):
        by_author = {}
        for m in self.messages:
            by_author.setdefault(m["author"], []).append(m)
        u101 = sorted(by_author[export.pseudo("101")], key=lambda m: int(m["rank"]))
        self.assertEqual([m["hour_utc"] for m in u101], ["2023-11-14T22:00Z", "2023-11-15T00:00Z"])
        u102 = sorted(by_author[export.pseudo("102")], key=lambda m: int(m["rank"]))
        self.assertEqual([m["reply_delay"] for m in u102], ["<15min", "15min-1h"])
        self.assertEqual(u102[0]["reply_to"], u101[0]["message"])
        self.assertEqual(u102[0]["mentions"], "1")

    def test_members_snapshot(self):
        snaps = list(self.out.glob("members_*.csv"))
        self.assertEqual(len(snaps), 1)
        members = {m["author"]: m for m in read_csv(snaps[0])}
        self.assertEqual(len(members), 3)  # bot and excluded member removed
        m101 = members[export.pseudo("101")]
        self.assertEqual(m101["joined_hour_utc"], "2023-11-14T21:00Z")
        self.assertEqual(m101["roles"], "autre;bénévole")  # rare role grouped, bot role dropped
        self.assertEqual(members[export.pseudo("102")]["roles"], "Groupe Lyon;bénévole")

    def test_incremental_and_purge(self):
        cache = Path(self.tmp.name) / "cache" / "messages" / "20.jsonl"
        self.assertEqual(len(cache.read_text().splitlines()), 4)
        with mock.patch.object(export, "get", fake_get):
            calls.clear()
            export.fetch("x", GUILD)
            self.assertIn(f"/channels/20/messages?limit=100&after={M4}", calls)
            self.assertEqual(len(cache.read_text().splitlines()), 4)
            with mock.patch.dict(os.environ, {"EXCLUDED_USER_IDS": "999,102"}):
                export.fetch("x", GUILD)
                export.build()
        self.assertEqual(len(cache.read_text().splitlines()), 2)
        authors = {m["author"] for m in read_csv(self.out / "messages.csv")}
        self.assertNotIn(export.pseudo("102"), authors)

    def test_load_env_parsing(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".env").write_text(
                'export DISCORD_BOT_TOKEN="tok"\nGUILD_ID=42  # comment\n# x=y\nPSEUDO_SALT=\'s#1\'\n')
            with mock.patch.object(export, "HERE", Path(d)), mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(export.load_env(), ("tok", "42"))
                self.assertEqual(os.environ["PSEUDO_SALT"], "s#1")


if __name__ == "__main__":
    unittest.main()
