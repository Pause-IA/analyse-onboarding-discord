#!/usr/bin/env python3
"""Read-only, pseudonymized export of the Pause IA Discord (member trajectories / Sankey).

Usage:
  python3 export.py check   # verify token and intents, list the channels the bot can read
  python3 export.py fetch   # update the technical cache (cache/), only new messages
  python3 export.py build   # build the deliverable for the analysis (out/) from the cache
  python3 export.py run     # fetch, then build

Two levels of files:
  cache/  stays with the tech team. Real message ids (needed to fetch only new
          messages) and exact timestamps. Authors are already pseudonymized.
  out/    the only thing sent for the analysis. No message text, no Discord ids
          that link back to a message, hours rounded, rare roles grouped.

Config in .env or environment (see README.md). Stdlib only.
"""
import csv
import hashlib
import hmac
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
API = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1420070400000

CATEGORY = 4
MESSAGE_TYPES = {0, 2, 5, 10, 11, 12}  # text, voice chat, announcement, threads
THREAD_TYPES = {10, 11, 12}
FORUM_TYPES = {15, 16}  # forum/media: messages live in their threads
HAS_THREADS = {0, 5, 15, 16}  # channels that can have archived threads
TYPE_NAMES = {0: "text", 2: "voice", 5: "announcement", 10: "thread", 11: "thread",
              12: "private_thread", 15: "forum", 16: "media"}
USER_MESSAGE_TYPES = {0, 19}  # default message, reply (used for `rank`)
REPLY = 19
HOUR = "%Y-%m-%dT%H:00Z"  # timestamps in the deliverable are rounded down to the hour
DELAY_BUCKETS = [(15 * 60, "<15min"), (3600, "15min-1h"), (6 * 3600, "1h-6h"),
                 (24 * 3600, "6h-24h")]


# ---------------------------------------------------------------- config

def load_env():
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.removeprefix("export ").split("=", 1)
            v = v.strip()
            if v[:1] in "\"'" and v[-1:] == v[:1] and len(v) > 1:
                v = v[1:-1]
            else:
                v = re.sub(r"\s+#.*$", "", v)
            os.environ.setdefault(k.strip(), v)
    for k in ("DISCORD_BOT_TOKEN", "GUILD_ID", "PSEUDO_SALT"):
        if not os.environ.get(k):
            sys.exit(f"Missing {k} in .env (see README.md)")
    return os.environ["DISCORD_BOT_TOKEN"], os.environ["GUILD_ID"]


def id_list(name):
    return {x.strip() for x in os.environ.get(name, "").split(",") if x.strip()}


def cache_dir():
    return Path(os.environ.get("CACHE_DIR", HERE / "cache"))


def out_dir():
    return Path(os.environ.get("OUT_DIR", HERE / "out"))


# ---------------------------------------------------------------- pseudonyms

def _hmac(value):
    salt = os.environ["PSEUDO_SALT"].encode()
    return hmac.new(salt, str(value).encode(), hashlib.sha256).hexdigest()[:12]


def pseudo(user_id):
    """Stable member code. Unchanged from the first version so codes stay the same."""
    return "u_" + _hmac(user_id)


def pseudo_msg(message_id):
    return "m_" + _hmac(f"message:{message_id}") if message_id else ""


def pseudo_thread(thread_id):
    return "t_" + _hmac(f"thread:{thread_id}") if thread_id else ""


def excluded_users():
    return {pseudo(u) for u in id_list("EXCLUDED_USER_IDS")}


# ---------------------------------------------------------------- Discord API

def get(path, token):
    """GET with rate-limit handling and a few retries.
    Returns parsed JSON, or None when Discord refuses (400/401/403/404)."""
    req = urllib.request.Request(API + path, headers={
        "Authorization": f"Bot {token}",
        "User-Agent": "DiscordBot (pauseia-diagnostic, 1.2)",
    })
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                if r.headers.get("X-RateLimit-Remaining") == "0":
                    time.sleep(float(r.headers.get("X-RateLimit-Reset-After", 1)))
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(float(json.load(e).get("retry_after", 1)) + 0.1)
            elif e.code in (400, 401, 403, 404):
                return None
            elif e.code >= 500 and attempt < 4:
                time.sleep(2 ** attempt)
            else:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Too many retries on {path}")


def slim(m):
    """What goes into the cache. No text, no attachments, no names."""
    is_reply = m["type"] == REPLY
    return {
        "id": m["id"],
        "ts": m["timestamp"],
        "type": m["type"],  # 7 = "X joined the server" system message
        "author": pseudo(m["author"]["id"]),
        "bot": m["author"].get("bot", False),
        "reply_to": (m.get("message_reference") or {}).get("message_id") if is_reply else None,
        "mentions": len(m.get("mentions", [])),
        "reactions": sum(r["count"] for r in m.get("reactions", [])),
        "thread": (m.get("thread") or {}).get("id"),
    }


def fetch_messages(channel, token, after=None):
    """All messages (after=None) or only those newer than `after`, oldest first."""
    out, cursor = [], after
    skip = excluded_users()
    while True:
        q = f"/channels/{channel['id']}/messages?limit=100"
        if after:
            batch = get(q + f"&after={cursor}", token)
        else:
            batch = get(q + (f"&before={cursor}" if cursor else ""), token)
        if not batch:
            break
        out += [s for s in map(slim, batch) if s["author"] not in skip]
        ids = [int(m["id"]) for m in batch]
        cursor = max(ids) if after else min(ids)
        print(f"\r  #{channel['name']}: {len(out)} messages", end="", flush=True)
    if out:
        print()
    return sorted(out, key=lambda m: int(m["id"]))


def archived_threads(channel, token):
    threads, before = [], None
    while True:
        q = f"/channels/{channel['id']}/threads/archived/public?limit=100" + (f"&before={before}" if before else "")
        page = get(q, token)
        if not page or not page["threads"]:
            break
        threads += page["threads"]
        if not page.get("has_more"):
            break
        before = page["threads"][-1]["thread_metadata"]["archive_timestamp"]
    return threads


def is_excluded(channel_id, by_id, excluded):
    """A channel is excluded if it, its parent channel or its category is listed."""
    seen = set()
    while channel_id and channel_id not in seen:
        if channel_id in excluded:
            return True
        seen.add(channel_id)
        channel_id = (by_id.get(channel_id) or {}).get("parent_id")
    return False


def all_channels(guild, token):
    """Guild channels (categories included) plus active and archived public threads."""
    channels = get(f"/guilds/{guild}/channels", token)
    if channels is None:
        sys.exit("Can't read the server: is the bot invited and the GUILD_ID right?")
    by_id = {c["id"]: c for c in channels}
    excluded = id_list("EXCLUDED_CHANNELS")
    result = list(channels)
    for c in channels:
        if c["type"] in HAS_THREADS and not is_excluded(c["id"], by_id, excluded):
            try:
                result += archived_threads(c, token)
            except Exception as e:  # one channel must not stop the whole export
                print(f"  !! #{c['name']}: archived threads skipped ({e})")
    active = (get(f"/guilds/{guild}/threads/active", token) or {}).get("threads", [])
    seen = {c["id"] for c in result}
    result += [t for t in active if t["id"] not in seen]
    return [{"id": c["id"], "name": c.get("name", ""), "type": c["type"],
             "parent_id": c.get("parent_id")} for c in result]


# ---------------------------------------------------------------- commands

def check(token, guild):
    me = get("/users/@me", token)
    if me is None:
        sys.exit("Token rejected.")
    print(f"Bot: {me['username']}")
    g = get(f"/guilds/{guild}?with_counts=true", token)
    if g is None:
        sys.exit("Bot is not in that server (yet).")
    print(f"Server: {g['name']} ({g.get('approximate_member_count')} members)")
    channels = get(f"/guilds/{guild}/channels", token) or []
    by_id = {c["id"]: c for c in channels}
    excluded = id_list("EXCLUDED_CHANNELS")
    sample = None
    print("Categories (an id in EXCLUDED_CHANNELS excludes all its channels):")
    for c in channels:
        if c["type"] == CATEGORY:
            print(f"  {c['id']}  {c['name']}")
    print("Channels (OK = readable, no = no access, -- = excluded):")
    for c in channels:
        if c["type"] not in MESSAGE_TYPES | FORUM_TYPES:
            continue
        if is_excluded(c["id"], by_id, excluded):
            print(f"  --  {c['id']}  #{c['name']} (excluded)")
            continue
        msgs = get(f"/channels/{c['id']}/messages?limit=5", token) if c["type"] in MESSAGE_TYPES else []
        category = (by_id.get(c.get("parent_id")) or {}).get("name", "")
        print(f"  {'OK ' if msgs is not None else 'no '} {c['id']}  #{c['name']}  [{category}]")
        if sample is None:
            sample = next((m for m in msgs or [] if m["type"] == 0 and not m["author"].get("bot")), None)
    if sample and sample.get("content"):
        print("\n!! Message content is readable: turn OFF 'Message Content Intent' in the "
              "Developer Portal (the bot must not read message text).")
    if get(f"/guilds/{guild}/members?limit=1", token) is None:
        print("\n!! Can't list members: turn on 'Server Members Intent' in the Developer Portal.")


def fetch(token, guild):
    cache = cache_dir()
    msg_dir = cache / "messages"
    msg_dir.mkdir(parents=True, exist_ok=True)
    purge_excluded(msg_dir)

    channels = all_channels(guild, token)
    (cache / "channels.json").write_text(json.dumps(channels, ensure_ascii=False, indent=1))
    by_id = {c["id"]: c for c in channels}
    excluded = id_list("EXCLUDED_CHANNELS")
    new = 0
    for c in channels:
        if c["type"] not in MESSAGE_TYPES or is_excluded(c["id"], by_id, excluded):
            continue
        f = msg_dir / f"{c['id']}.jsonl"
        lines = f.read_text().splitlines() if f.exists() else []
        after = json.loads(lines[-1])["id"] if lines else None
        try:
            msgs = fetch_messages(c, token, after)
        except Exception as e:  # one channel must not stop the whole export
            print(f"\n  !! #{c['name']}: skipped ({e})")
            continue
        with f.open("a") as out:
            out.writelines(json.dumps(m) + "\n" for m in msgs)
        new += len(msgs)

    roles = get(f"/guilds/{guild}/roles", token) or []
    (cache / "roles.json").write_text(json.dumps(
        {r["id"]: {"name": r["name"], "managed": r.get("managed", False)} for r in roles},
        ensure_ascii=False, indent=1))

    # Members (needs Server Members intent). One dated snapshot per run, so that
    # comparing snapshots shows departures and role changes over time.
    members, after = [], "0"
    skip = excluded_users()
    while True:
        page = get(f"/guilds/{guild}/members?limit=1000&after={after}", token)
        if not page:
            break
        members += [{"id": pseudo(m["user"]["id"]), "joined_at": m["joined_at"],
                     "bot": m["user"].get("bot", False), "roles": m["roles"]}
                    for m in page if pseudo(m["user"]["id"]) not in skip]
        after = page[-1]["user"]["id"]
    if members:
        (cache / f"members_{date.today().isoformat()}.json").write_text(json.dumps(members))
    print(f"{new} new messages, {len(members)} members → {cache}")


def purge_excluded(msg_dir):
    """Remove from the cache the messages of members who asked to be excluded."""
    skip = excluded_users()
    if not skip:
        return
    for f in msg_dir.glob("*.jsonl"):
        lines = f.read_text().splitlines()
        kept = [l for l in lines if json.loads(l)["author"] not in skip]
        if len(kept) != len(lines):
            f.write_text("".join(l + "\n" for l in kept))
    for f in msg_dir.parent.glob("members_*.json"):
        members = json.loads(f.read_text())
        kept = [m for m in members if m["id"] not in skip]
        if len(kept) != len(members):
            f.write_text(json.dumps(kept))


# ---------------------------------------------------------------- deliverable

def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def snowflake_time(snowflake):
    return datetime.fromtimestamp(((int(snowflake) >> 22) + DISCORD_EPOCH_MS) / 1000, timezone.utc)


def delay_bucket(seconds):
    for limit, label in DELAY_BUCKETS:
        if seconds < limit:
            return label
    return ">24h"


def write_csv(path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def build(token=None, guild=None):
    cache, out = cache_dir(), out_dir()
    if not (cache / "channels.json").exists():
        sys.exit(f"No cache in {cache}: run `python3 export.py fetch` first.")
    # Empty the folder instead of deleting it: with Docker, /out is a mount point
    # and cannot be removed.
    out.mkdir(parents=True, exist_ok=True)
    for f in out.iterdir():
        shutil.rmtree(f) if f.is_dir() else f.unlink()

    skip = excluded_users()
    excluded_channels = id_list("EXCLUDED_CHANNELS")
    channels = json.loads((cache / "channels.json").read_text())
    by_id = {c["id"]: c for c in channels}

    def public_id(c):
        return pseudo_thread(c["id"]) if c["type"] in THREAD_TYPES else c["id"]

    def category_of(c):
        while c and c["type"] != CATEGORY:
            c = by_id.get(c.get("parent_id"))
        return c["name"] if c else ""

    # messages.csv
    messages, seen = [], set()
    for f in sorted((cache / "messages").glob("*.jsonl")):
        c = by_id.get(f.stem)
        if not c or is_excluded(c["id"], by_id, excluded_channels):
            continue
        for line in f.read_text().splitlines():
            m = json.loads(line)
            if m["author"] in skip or m["id"] in seen:
                continue
            seen.add(m["id"])
            m["_channel"] = c
            m["_dt"] = parse_ts(m["ts"])
            messages.append(m)

    # channels.csv: only channels that appear in the export, so that the names of
    # channels the bot cannot read (or that are excluded) never leave the server.
    # Thread names are dropped (they often contain a member's name).
    used = {m["_channel"]["id"] for m in messages}
    used |= {by_id[i].get("parent_id") for i in list(used) if by_id[i]["type"] in THREAD_TYPES}
    rows = []
    for c in channels:
        if c["id"] not in used:
            continue
        thread = c["type"] in THREAD_TYPES
        rows.append([public_id(c), "" if thread else c["name"], TYPE_NAMES.get(c["type"], str(c["type"])),
                     category_of(c), c.get("parent_id") if thread else ""])
    write_csv(out / "channels.csv", ["channel", "name", "type", "category", "parent_channel"], rows)

    # rank = position of the message in its author's history (exact order, before rounding)
    messages.sort(key=lambda m: (m["_dt"], int(m["id"])))
    count = Counter()
    for m in messages:
        if m["type"] in USER_MESSAGE_TYPES:
            count[m["author"]] += 1
            m["_rank"] = count[m["author"]]

    rows = []
    for m in messages:
        c = m["_channel"]
        thread = c["type"] in THREAD_TYPES
        delay = ""
        if m.get("reply_to"):
            delay = delay_bucket((m["_dt"] - snowflake_time(m["reply_to"])).total_seconds())
        rows.append([
            pseudo_msg(m["id"]),
            m["_dt"].strftime(HOUR),
            public_id(c),
            c.get("parent_id") if thread else "",
            m["type"],
            m["author"],
            int(m["bot"]),
            m.get("_rank", ""),
            pseudo_msg(m.get("reply_to")),
            delay,
            m["mentions"],
            m["reactions"],
            pseudo_thread(m.get("thread")),
        ])
    # sort on the rounded hour only, so the file does not give the exact order within an hour
    rows.sort(key=lambda r: (r[1], r[2], r[0]))
    write_csv(out / "messages.csv",
              ["message", "hour_utc", "channel", "parent_channel", "type", "author", "bot", "rank",
               "reply_to", "reply_delay", "mentions", "reactions", "thread_started"], rows)

    # members_<date>.csv: one per snapshot. Rare roles are grouped into "autre".
    roles = json.loads((cache / "roles.json").read_text()) if (cache / "roles.json").exists() else {}
    keep_always = id_list("ROLES_ALWAYS_KEEP")
    min_members = int(os.environ.get("ROLE_MIN_MEMBERS", 5))
    snapshots = {}
    for f in sorted(cache.glob("members_*.json")):
        snapshots[f.stem.removeprefix("members_")] = [
            m for m in json.loads(f.read_text()) if not m["bot"] and m["id"] not in skip]
    peak = Counter()  # most holders a role ever had, so a role is grouped the same way in every snapshot
    for members in snapshots.values():
        for role, n in Counter(r for m in members for r in m["roles"]).items():
            peak[role] = max(peak[role], n)

    def role_label(role_id):
        r = roles.get(role_id)
        if not r or r["managed"]:
            return None  # bot/integration roles
        if role_id in keep_always or r["name"] in keep_always or peak[role_id] >= min_members:
            return r["name"]
        return "autre"

    for day, members in snapshots.items():
        rows = []
        for m in members:
            labels = sorted({l for l in map(role_label, m["roles"]) if l})
            joined = parse_ts(m["joined_at"]).strftime(HOUR) if m.get("joined_at") else ""
            rows.append([m["id"], joined, ";".join(labels)])
        rows.sort()
        write_csv(out / f"members_{day}.csv", ["author", "joined_hour_utc", "roles"], rows)

    print(f"{len(messages)} messages, {len(snapshots)} member snapshot(s) → {out}")


def run(token, guild):
    fetch(token, guild)
    build()


if __name__ == "__main__":
    commands = {"check": check, "fetch": fetch, "build": build, "run": run}
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd not in commands:
        sys.exit(__doc__)
    token, guild = load_env()
    commands[cmd](token, guild)
