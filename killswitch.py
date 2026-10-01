#!/usr/bin/env python3
"""
ntfy-driven kill switch for YOUR OWN Render / GitHub / Netlify / Supabase accounts.

Conversation (all on the CMD topic, bot answers on the OUT topic):

  you : muttonsoup            -> bot asks for flavour
  you : render                -> bot asks for spice   (flavour = render|github|netlify|supabase ac1|supabase ac2|all)
  you : menu                  -> bot lists all projects of all services (any time after the trigger)
  you : proj1 , proj2         -> deletes those projects inside the chosen flavour
  you : level 5 spice         -> bot asks "sure?" ; you : yes  -> deletes EVERYTHING in that flavour
  you : cancel                -> abort at any point

  muttontest instead of muttonsoup = always a dry run (lists + reports, deletes nothing).
  MODE=test (default) makes even muttonsoup a dry run. Set MODE=live only when you're ready.

State is rebuilt from ntfy history on every poll (no database). Each handled message gets
acked via a tag on the OUT topic, so nothing is ever executed twice.
"""
import json
import os
import sys
import time

import requests


def env(name, default=""):
    return os.environ.get(name, default).strip()


NTFY_SERVER = env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
CMD_TOPIC = env("NTFY_CMD_TOPIC")
OUT_TOPIC = env("NTFY_OUT_TOPIC")
NTFY_TOKEN = env("NTFY_TOKEN")
TRIGGER = env("TRIGGER_WORD", "muttonsoup").lower()
TEST_TRIGGER = env("TEST_TRIGGER_WORD", "muttontest").lower()
PING_WORD = env("PING_WORD", "shop open").lower()  # "shop open?" (trailing ?/! ignored)
LIVE = env("MODE", "test").lower() == "live"
LOOKBACK = env("LOOKBACK", "30m")            # how far back to read ntfy history
SESSION_TTL = int(env("SESSION_TTL", "600"))  # max gap (s) between messages of one session
STALE_AFTER = int(env("STALE_AFTER", "600"))  # never act on commands older than this (s)
LOOP_SECONDS = int(env("LOOP_SECONDS", "240"))
POLL_EVERY = int(env("POLL_EVERY", "10"))
SELF_REPO = env("GITHUB_REPOSITORY").lower()  # owner/repo of this kill switch (deleted last)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def norm(s):
    return " ".join((s or "").lower().split())


# --------------------------------------------------------------------------- http
def http(method, url, headers=None, **kw):
    last = None
    for attempt in range(3):
        try:
            r = requests.request(method, url, headers=headers, timeout=30, **kw)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(2)
    raise last


# --------------------------------------------------------------------------- ntfy
def ntfy_headers(extra=None):
    h = dict(extra or {})
    if NTFY_TOKEN:
        h["Authorization"] = f"Bearer {NTFY_TOKEN}"
    return h


def ntfy_poll(topic):
    r = requests.get(f"{NTFY_SERVER}/{topic}/json",
                     params={"poll": "1", "since": LOOKBACK},
                     headers=ntfy_headers(), timeout=30)
    r.raise_for_status()
    msgs = []
    for line in r.text.splitlines():
        if line.strip():
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("event") == "message":
                msgs.append(m)
    msgs.sort(key=lambda m: m.get("time", 0))  # stable: keeps server order within a second
    return msgs


def say(text, ack_id=None):
    """Send text to the OUT topic (chunked). The first chunk carries the ack tag."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3500:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for c in chunks:
        headers = {"Title": "killswitch" + (" LIVE" if LIVE else " test")}
        if ack_id:
            headers["Tags"] = f"ack_{ack_id}"
            ack_id = None
        r = requests.post(f"{NTFY_SERVER}/{OUT_TOPIC}", data=c.strip().encode("utf-8"),
                          headers=ntfy_headers(headers), timeout=20)
        r.raise_for_status()  # if this fails we abort BEFORE doing anything destructive


# --------------------------------------------------------------------------- providers
# Every provider: list() -> (items, warnings); delete(item) -> (ok, message)
# item = {"id", "name", "keys": set of lowercase match names, "self": bool, ...}
class Render:
    BASE = "https://api.render.com/v1"
    KINDS = [("/services", "service", True), ("/postgres", "postgres", False),
             ("/key-value", "keyValue", False)]

    def __init__(self, key):
        self.h = {"Authorization": f"Bearer {key}", "Accept": "application/json"}

    def list(self):
        items, warns = [], []
        for path, field, required in self.KINDS:
            cursor = None
            try:
                while True:
                    params = {"limit": 100}
                    if cursor:
                        params["cursor"] = cursor
                    r = http("GET", self.BASE + path, self.h, params=params)
                    if r.status_code != 200:
                        raise RuntimeError(f"HTTP {r.status_code} {r.text[:120]}")
                    rows = r.json()
                    for row in rows:
                        o = row.get(field) or row
                        items.append({"id": o["id"], "name": o["name"], "path": path,
                                      "keys": {o["name"].lower()}, "self": False})
                    if len(rows) < 100:
                        break
                    cursor = rows[-1].get("cursor")
            except Exception as e:
                if required:
                    raise
                warns.append(f"could not list render {path}: {e}")
        return items, warns

    def delete(self, it):
        r = http("DELETE", f"{self.BASE}{it['path']}/{it['id']}", self.h)
        return r.status_code in (200, 202, 204, 404), f"HTTP {r.status_code}"


class GitHub:
    BASE = "https://api.github.com"

    def __init__(self, token):
        self.h = {"Authorization": f"Bearer {token}",
                  "Accept": "application/vnd.github+json",
                  "X-GitHub-Api-Version": "2022-11-28"}

    def list(self):
        items, page = [], 1
        while True:
            r = http("GET", f"{self.BASE}/user/repos", self.h,
                     params={"per_page": 100, "page": page, "affiliation": "owner"})
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code} {r.text[:120]}")
            rows = r.json()
            for o in rows:
                full = o["full_name"]
                items.append({"id": full, "name": o["name"],
                              "keys": {o["name"].lower(), full.lower()},
                              "self": full.lower() == SELF_REPO})
            if len(rows) < 100:
                break
            page += 1
        return items, []

    def delete(self, it):
        r = http("DELETE", f"{self.BASE}/repos/{it['id']}", self.h)
        return r.status_code in (204, 404), f"HTTP {r.status_code} {'' if r.status_code in (204, 404) else r.text[:120]}"


class Netlify:
    BASE = "https://api.netlify.com/api/v1"

    def __init__(self, token):
        self.h = {"Authorization": f"Bearer {token}"}

    def list(self):
        items, page = [], 1
        while True:
            r = http("GET", f"{self.BASE}/sites", self.h, params={"per_page": 100, "page": page})
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code} {r.text[:120]}")
            rows = r.json()
            for o in rows:
                items.append({"id": o["id"], "name": o["name"],
                              "keys": {o["name"].lower()}, "self": False})
            if len(rows) < 100:
                break
            page += 1
        return items, []

    def delete(self, it):
        r = http("DELETE", f"{self.BASE}/sites/{it['id']}", self.h)
        return r.status_code in (200, 204, 404), f"HTTP {r.status_code}"


class Supabase:
    BASE = "https://api.supabase.com/v1"

    def __init__(self, token):
        self.h = {"Authorization": f"Bearer {token}"}

    def list(self):
        r = http("GET", f"{self.BASE}/projects", self.h)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} {r.text[:120]}")
        items = [{"id": o["id"], "name": o["name"],
                  "keys": {o["name"].lower(), o["id"].lower()}, "self": False}
                 for o in r.json()]
        return items, []

    def delete(self, it):
        r = http("DELETE", f"{self.BASE}/projects/{it['id']}", self.h)
        return r.status_code in (200, 204, 404), f"HTTP {r.status_code}"


def build_providers():
    p = {}
    if env("RENDER_API_KEY"):
        p["render"] = Render(env("RENDER_API_KEY"))
    if env("GH_PAT"):
        p["github"] = GitHub(env("GH_PAT"))
    if env("NETLIFY_TOKEN"):
        p["netlify"] = Netlify(env("NETLIFY_TOKEN"))
    if env("SUPABASE_TOKEN_AC1"):
        p["supabase ac1"] = Supabase(env("SUPABASE_TOKEN_AC1"))
    if env("SUPABASE_TOKEN_AC2"):
        p["supabase ac2"] = Supabase(env("SUPABASE_TOKEN_AC2"))
    return p


# --------------------------------------------------------------------------- conversation
def step(state, ctx, text, flavours):
    """Pure state machine. Returns (state, ctx, actions)."""
    if text.rstrip("?! ") == PING_WORD:  # "shop open?" works anytime, never disturbs a session
        return state, ctx, [("ping",)]
    if text in (TRIGGER, TEST_TRIGGER):
        dry = (text == TEST_TRIGGER) or not LIVE
        mode = "TEST MODE - nothing will be deleted" if dry else "LIVE"
        opts = ", ".join(flavours + ["all"])
        return "FLAVOUR", {"dry": dry}, [("say", f"[{mode}]\nWhich flavour?\n{opts}")]
    if state == "IDLE":
        return state, ctx, []
    if text == "cancel":
        return "IDLE", {}, [("say", "Cancelled.")]

    if state == "FLAVOUR":
        if text == "menu":
            return state, ctx, [("menu",)]
        if text in flavours or text == "all":
            ctx = dict(ctx, flavour=text)
            return "SPICE", ctx, [("say",
                f"Flavour: {text}\nWhat spice?\n- menu = list all projects\n"
                "- names separated by ' , ' = delete those\n- level 5 spice = delete everything")]
        return state, ctx, [("say", "Didn't get that. Pick a flavour, or 'menu', or 'cancel'.")]

    if state == "SPICE":
        if text == "menu":
            return state, ctx, [("menu",)]
        if text in ("level 5 spice", "level 5", "level5 spice", "level5"):
            return "CONFIRM", ctx, [("say",
                f"LEVEL 5 on '{ctx['flavour']}': this deletes EVERYTHING there"
                f"{' (test mode: dry run)' if ctx.get('dry') else ''}.\nReply 'yes' to confirm, anything else aborts.")]
        names = [n.strip() for n in text.split(",") if n.strip()]
        if not names:
            return state, ctx, [("say", "Send names separated by ' , ', 'menu', or 'level 5 spice'.")]
        return "IDLE", {}, [("execute", {"flavour": ctx["flavour"], "names": names, "dry": ctx.get("dry", True)})]

    if state == "CONFIRM":
        if text == "yes":
            return "IDLE", {}, [("execute", {"flavour": ctx["flavour"], "names": None, "dry": ctx.get("dry", True)})]
        return "IDLE", {}, [("say", "Aborted. Nothing deleted.")]

    return "IDLE", {}, []


def send_menu(providers, out):
    for name, p in providers.items():
        try:
            items, warns = p.list()
        except Exception as e:
            out(f"== {name} ==\nERROR listing: {e}")
            continue
        names = sorted({it["name"] for it in items}, key=str.lower)
        body = [f"== {name} =="] + (names or ["(nothing)"]) + [f"warning: {w}" for w in warns]
        if name == "github" and SELF_REPO:
            body.append(f"(kill switch repo = {SELF_REPO}, always deleted last)")
        out("\n".join(body))


def do_execute(providers, spec, out):
    dry = spec["dry"] or not LIVE
    tag = "TEST - nothing is deleted" if dry else "LIVE"
    wanted = None if spec["names"] is None else {n.lower() for n in spec["names"]}
    flavours = list(providers) if spec["flavour"] == "all" else [spec["flavour"]]
    flavours.sort(key=lambda f: f == "github")  # github always last
    what = "EVERYTHING" if wanted is None else ", ".join(sorted(wanted))
    out(f"[{tag}] starting: {spec['flavour']} / {what}")

    matched, self_job = set(), None
    for f in flavours:
        p = providers[f]
        try:
            items, warns = p.list()
        except Exception as e:
            out(f"== {f} ==\nCould not list projects: {e}")
            continue
        lines = [f"== {f} =="] + [f"warning: {w}" for w in warns]
        did = 0
        for it in items:
            if wanted is not None:
                hit = it["keys"] & wanted
                if not hit:
                    continue
                matched |= hit
            if it.get("self"):
                self_job = (f, p, it)
                continue
            did += 1
            if dry:
                lines.append(f"WOULD DELETE {it['name']}")
            else:
                try:
                    ok, msg = p.delete(it)
                except Exception as e:
                    ok, msg = False, str(e)
                lines.append(f"{'deleted' if ok else 'FAILED'} {it['name']} ({msg})")
        if not did:
            lines.append("(nothing to do)")
        out("\n".join(lines))

    if wanted is not None:
        missing = sorted(wanted - matched)
        if missing:
            out("Not found: " + ", ".join(missing))

    if self_job:
        f, p, it = self_job
        if dry:
            out(f"WOULD DELETE kill switch repo {it['id']} (last)")
        else:
            out(f"Deleting the kill switch repo {it['id']} now (last).")
            try:
                ok, msg = p.delete(it)
            except Exception as e:
                ok, msg = False, str(e)
            try:
                out(f"kill switch repo {'deleted' if ok else 'FAILED'} ({msg})")
            except Exception:
                pass
    out(f"[{tag}] done.")


def do_ping(providers, out):
    lines = [f"Shop is OPEN. [{'LIVE' if LIVE else 'TEST MODE'}] {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}"]
    for name, p in providers.items():
        try:
            items, _ = p.list()  # read-only: proves the token works
            lines.append(f"- {name}: ok ({len(items)} projects)")
        except Exception as e:
            lines.append(f"- {name}: PROBLEM {str(e)[:120]}")
    if not providers:
        lines.append("- no providers loaded (check secrets)")
    out("\n".join(lines))


def perform(actions, msg_id, providers):
    first = [msg_id]

    def out(text):
        say(text, first.pop() if first else None)

    for a in actions:
        if a[0] == "say":
            out(a[1])
        elif a[0] == "menu":
            send_menu(providers, out)
        elif a[0] == "ping":
            do_ping(providers, out)
        elif a[0] == "execute":
            do_execute(providers, a[1], out)


def process(cmds, acked, providers, now):
    state, ctx, prev_t = "IDLE", {}, None
    for m in cmds:
        t = m.get("time", 0)
        text = norm(m.get("message", ""))
        if prev_t is not None and t - prev_t > SESSION_TTL:
            state, ctx = "IDLE", {}
        prev_t = t
        state, ctx, actions = step(state, ctx, text, list(providers))
        if not actions or m["id"] in acked:
            continue
        if now - t > STALE_AFTER and actions[0][0] != "ping":
            say("Ignored an old command (too stale to run safely). Start again.", m["id"])
            state, ctx = "IDLE", {}
            continue
        perform(actions, m["id"], providers)


def run_once(providers):
    cmds = ntfy_poll(CMD_TOPIC)
    if not cmds:
        return
    outs = ntfy_poll(OUT_TOPIC)
    acked = {t[4:] for m in outs for t in m.get("tags", []) if t.startswith("ack_")}
    process(cmds, acked, providers, time.time())


def main():
    if not CMD_TOPIC or not OUT_TOPIC:
        sys.exit("NTFY_CMD_TOPIC and NTFY_OUT_TOPIC must be set")
    providers = build_providers()
    log(f"mode={'LIVE' if LIVE else 'TEST'} providers={list(providers)}")
    end = time.time() + LOOP_SECONDS
    while True:
        try:
            run_once(providers)
        except Exception as e:  # never die on a transient error; next poll retries
            log("poll error:", repr(e))
        if time.time() >= end:
            break
        time.sleep(POLL_EVERY)


if __name__ == "__main__":
    main()
