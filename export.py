#!/usr/bin/env python3
"""Read-only, pseudonymized export of the Pause IA Discord (member trajectories / Sankey).

Usage:
  python3 export.py check    # verify token, list the channels the bot can read
  python3 export.py export   # export everything to data/ (rerun = only new messages)
  python3 export.py loop     # export, then again every INTERVAL_HOURS (for Docker)

Config in .env or environment (see README.md). Stdlib only.
User IDs are replaced by HMAC(PSEUDO_SALT, id): stable across runs, not reversible
without the salt. Usernames are never written.
"""
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR", HERE / "data"))
API = "https://discord.com/api/v10"

# Channel types that hold messages: text, voice-chat, announcement, threads
MESSAGE_TYPES = {0, 2, 5, 10, 11, 12}
FORUM_TYPES = {15, 16}  # forum/media: messages live in their threads
USER_MENTION = re.compile(r"<@!?(\d+)>")


def load_env():
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    for k in ("DISCORD_BOT_TOKEN", "GUILD_ID", "PSEUDO_SALT"):
        if not os.environ.get(k):
            sys.exit(f"Missing {k} in .env (see README.md)")
    return os.environ["DISCORD_BOT_TOKEN"], os.environ["GUILD_ID"]


def pseudo(user_id):
    salt = os.environ["PSEUDO_SALT"].encode()
    return "u_" + hmac.new(salt, str(user_id).encode(), hashlib.sha256).hexdigest()[:12]


def get(path, token):
    """GET with rate-limit handling. Returns parsed JSON, or None on 401/403/404."""
    req = urllib.request.Request(API + path, headers={
        "Authorization": f"Bot {token}",
        "User-Agent": "DiscordBot (pauseia-diagnostic, 1.1)",
    })
    while True:
        try:
            with urllib.request.urlopen(req) as r:
                if r.headers.get("X-RateLimit-Remaining") == "0":
                    time.sleep(float(r.headers.get("X-RateLimit-Reset-After", 1)))
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(float(json.load(e).get("retry_after", 1)) + 0.1)
            elif e.code in (401, 403, 404):
                return None
            else:
                raise


def slim(m, channel):
    """Keep what the analysis needs, pseudonymize people, drop avatars/embeds/etc."""
    content = ""
    if os.environ.get("INCLUDE_CONTENT", "1") == "1":
        content = USER_MENTION.sub(lambda x: f"<@{pseudo(x.group(1))}>", m.get("content", ""))
    return {
        "id": m["id"],
        "channel_id": channel["id"],
        "parent_id": channel.get("parent_id"),
        "timestamp": m["timestamp"],
        "type": m["type"],  # 7 = "X joined the server" system message
        "author": pseudo(m["author"]["id"]),
        "bot": m["author"].get("bot", False),
        "content": content,
        "mentions": [pseudo(u["id"]) for u in m.get("mentions", [])],
        "reply_to": (m.get("message_reference") or {}).get("message_id"),
        "reactions": sum(r["count"] for r in m.get("reactions", [])),
        "attachments": len(m.get("attachments", [])),
        "thread_id": (m.get("thread") or {}).get("id"),
    }


def fetch_messages(channel, token, after=None):
    """All messages (after=None) or only those newer than `after`, oldest first."""
    out, cursor = [], after
    while True:
        q = f"/channels/{channel['id']}/messages?limit=100"
        if after:
            batch = get(q + f"&after={cursor}", token)
        else:
            batch = get(q + (f"&before={cursor}" if cursor else ""), token)
        if not batch:
            break
        out += [slim(m, channel) for m in batch]
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


def all_channels(guild, token):
    channels = get(f"/guilds/{guild}/channels", token)
    if channels is None:
        sys.exit("Can't read the server: is the bot invited and the GUILD_ID right?")
    active = (get(f"/guilds/{guild}/threads/active", token) or {}).get("threads", [])
    visible = []
    for c in channels:
        if c["type"] in MESSAGE_TYPES or c["type"] in FORUM_TYPES:
            visible.append(c)
            visible += archived_threads(c, token)
    seen = {c["id"] for c in visible}
    return visible + [t for t in active if t["id"] not in seen]


def check(token, guild):
    me = get("/users/@me", token)
    if me is None:
        sys.exit("Token rejected.")
    print(f"Bot: {me['username']}")
    g = get(f"/guilds/{guild}?with_counts=true", token)
    if g is None:
        sys.exit("Bot is not in that server (yet).")
    print(f"Server: {g['name']} ({g.get('approximate_member_count')} members)")
    for c in get(f"/guilds/{guild}/channels", token):
        if c["type"] in MESSAGE_TYPES | FORUM_TYPES:
            readable = get(f"/channels/{c['id']}/messages?limit=1", token) is not None
            print(f"  {'OK ' if readable else 'no '} #{c['name']}")
    sample = next((m for c in get(f"/guilds/{guild}/channels", token) if c["type"] == 0
                   for m in (get(f"/channels/{c['id']}/messages?limit=5", token) or [])
                   if m["type"] == 0 and not m["author"].get("bot")), None)
    if sample and not sample.get("content") and not sample.get("attachments"):
        print("\n!! Message content is empty: turn on 'Message Content Intent' in the Developer Portal.")


def export(token, guild):
    DATA.mkdir(parents=True, exist_ok=True)
    channels = all_channels(guild, token)
    (DATA / "_channels.json").write_text(json.dumps(
        [{"id": c["id"], "name": c["name"], "type": c["type"], "parent_id": c.get("parent_id")}
         for c in channels], ensure_ascii=False, indent=1))
    new = 0
    for c in channels:
        if c["type"] in FORUM_TYPES:
            continue  # forum parents hold no messages, their threads do
        f = DATA / f"{c['id']}.jsonl"
        lines = f.read_text().splitlines() if f.exists() else []
        after = json.loads(lines[-1])["id"] if lines else None
        msgs = fetch_messages(c, token, after)
        with f.open("a") as out:
            out.writelines(json.dumps(m, ensure_ascii=False) + "\n" for m in msgs)
        new += len(msgs)
    # Roles (names are not personal) and member join dates (needs Server Members intent)
    roles = get(f"/guilds/{guild}/roles", token) or []
    (DATA / "_roles.json").write_text(json.dumps(
        {r["id"]: r["name"] for r in roles}, ensure_ascii=False, indent=1))
    members, after = [], "0"
    while True:
        page = get(f"/guilds/{guild}/members?limit=1000&after={after}", token)
        if not page:
            break
        members += [{"id": pseudo(m["user"]["id"]), "joined_at": m["joined_at"],
                     "bot": m["user"].get("bot", False), "roles": m["roles"]} for m in page]
        after = page[-1]["user"]["id"]
    if members:
        (DATA / "_members.json").write_text(json.dumps(members))
    print(f"{new} new messages, {len(members)} members → {DATA}")


def loop(token, guild):
    hours = float(os.environ.get("INTERVAL_HOURS", 24))
    while True:
        export(token, guild)
        time.sleep(hours * 3600)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    token, guild = load_env()
    {"check": check, "export": export, "loop": loop}[cmd](token, guild)
