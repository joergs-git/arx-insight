#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ARX Insight - who may do what (v0.6.0): access links for the phone, scopes, limits.

The app is local: on this machine everything is allowed. Phone access is an OPT-IN second listener
in the local network (arx_lan); whoever connects there needs a token, handed over as a QR code:

  trainer   one token for the owner / trainer: everything a phone should be able to do. Still
            PC-only: API key, update, shutdown, database path, phone access itself and its links.
  athlete   one token per person: that person's own report, check-in, profile / goals, coach
            board and chat - with a small daily AI allowance, because the owner's key pays for it.
            It expires after ATHLETE_DAYS, can be renewed, replaced or revoked at the PC, and a
            minor's link has the chat switched off until the trainer turns it on.

Tokens are random (192 bit) and live in access.json in the data folder, next to config.json (which
holds the API key) - readable, so the QR code can be shown again. They travel in the URL FRAGMENT
of the QR code (never sent to a server, never in a log) and afterwards in the X-ARX-Token header.
The route table in arx_app decides what a role may do; this module only answers "who is this?".

Leaf module: imports arx_base only. Public domain / CC0. No warranty.
"""
from __future__ import annotations
import os, time, secrets, threading
from datetime import date, timedelta
from typing import NamedTuple

from arx_base import data_dir, write_json_atomic, _today

ATHLETE_DAYS = 90                  # an athlete link is valid this long (renewable at the PC)
ATHLETE_BOARDS_PER_DAY = 3         # AI allowance of one athlete link per day: coach boards ...
ATHLETE_QUESTIONS_PER_DAY = 20     # ... and chat questions (the owner's key pays)
BAD_TOKEN_MAX = 8                  # wrong tokens from one address within BAD_TOKEN_WINDOW_S ...
BAD_TOKEN_WINDOW_S = 300
BAD_TOKEN_BLOCK_S = 300            # ... block that address for this long (429)
TICKET_TTL_S = 120                 # a download ticket is good for one request within two minutes
TICKETS_MAX = 8
SEEN_KEEP_S = 600                  # "connected devices" forgets an address after ten minutes
RANK = {"open": 0, "athlete": 1, "trainer": 2, "local": 3}


class Identity(NamedTuple):
    role: str                      # local | trainer | athlete
    user_id: int | None = None     # the one person an athlete link belongs to
    expires: str | None = None     # ISO date (athlete links)
    chat: bool = True              # may this link talk to the coach?

    def allows(self, perm: str) -> bool:
        return RANK[self.role] >= RANK[perm]


LOCAL = Identity("local")


def _new_token(prefix: str) -> str:
    return f"{prefix}-{secrets.token_urlsafe(24)}"


class AccessStore:
    """access.json: {lan: {enabled, ip}, trainer: {token, created}, athletes: {user_id: {token,
    created, expires, chat, usage: {date, boards, questions}}}}. Every change is one locked
    read-modify-write with an atomic file swap."""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(data_dir(), "access.json")
        self._lock = threading.RLock()

    # -- file -------------------------------------------------------------------------------------
    def _load(self) -> dict:
        import json
        try:
            with open(self.path, encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("lan", {"enabled": False, "ip": "auto"})
        data.setdefault("athletes", {})
        return data

    def _change(self, fn):
        with self._lock:
            data = self._load()
            out = fn(data)
            write_json_atomic(self.path, data)
            return out

    # -- phone access on / off (remembered across restarts: a kiosk PC reboots) ----------------------
    def lan(self) -> dict:
        with self._lock:
            return dict(self._load()["lan"])

    def set_lan(self, enabled: bool, ip: str | None = None) -> dict:
        def fn(d):
            d["lan"]["enabled"] = bool(enabled)
            if ip:
                d["lan"]["ip"] = ip
            return dict(d["lan"])
        return self._change(fn)

    # -- trainer ------------------------------------------------------------------------------------
    def trainer_token(self, rotate: bool = False) -> str:
        """The trainer token (created on first use). rotate=True replaces it: every phone that
        carries the old one is logged out."""
        def fn(d):
            if rotate or not (d.get("trainer") or {}).get("token"):
                d["trainer"] = {"token": _new_token("T"), "created": _today({}).isoformat()}
            return d["trainer"]["token"]
        return self._change(fn)

    # -- athletes -------------------------------------------------------------------------------------
    @staticmethod
    def _public(rec: dict | None, today: date) -> dict | None:
        if not rec:
            return None
        return {"token": rec["token"], "created": rec.get("created"), "expires": rec.get("expires"),
                "expired": bool(rec.get("expires")) and rec["expires"] < today.isoformat(),
                "chat": bool(rec.get("chat", True)), "usage": AccessStore._usage_of(rec, today)}

    @staticmethod
    def _usage_of(rec: dict, today: date) -> dict:
        u = rec.get("usage") or {}
        if u.get("date") != today.isoformat():
            u = {"date": today.isoformat(), "boards": 0, "questions": 0}
        return u

    def athlete(self, user_id: int, create: bool = False, minor: bool = False, rotate: bool = False) -> dict | None:
        """The link of one person. create: make one when there is none; rotate: a new token (the old
        phone is logged out). A minor's new link starts with the chat switched off."""
        today = _today({})

        def fn(d):
            rec = d["athletes"].get(str(user_id))
            if (create and not rec) or rotate:
                rec = {"token": _new_token("A"), "created": today.isoformat(),
                       "expires": (today + timedelta(days=ATHLETE_DAYS)).isoformat(),
                       "chat": rec.get("chat", not minor) if rec else (not minor)}      # a replaced link keeps its switch
                d["athletes"][str(user_id)] = rec
            return self._public(rec, today)
        return self._change(fn)

    def renew_athlete(self, user_id: int) -> dict | None:
        today = _today({})

        def fn(d):
            rec = d["athletes"].get(str(user_id))
            if rec:
                rec["expires"] = (today + timedelta(days=ATHLETE_DAYS)).isoformat()
            return self._public(rec, today)
        return self._change(fn)

    def revoke_athlete(self, user_id: int) -> bool:
        return bool(self._change(lambda d: d["athletes"].pop(str(user_id), None)))

    def set_chat(self, user_id: int, on: bool) -> dict | None:
        today = _today({})

        def fn(d):
            rec = d["athletes"].get(str(user_id))
            if rec:
                rec["chat"] = bool(on)
            return self._public(rec, today)
        return self._change(fn)

    # -- who is this? ---------------------------------------------------------------------------------
    def identify(self, token: str | None) -> Identity | None:
        """Identity of a token from the network - None for unknown, malformed or expired ones (the
        caller answers all of them alike). Every stored token is compared in constant time."""
        if not token or not isinstance(token, str) or len(token) > 80:
            return None
        today = _today({}).isoformat()
        with self._lock:
            data = self._load()
        found = None
        t = (data.get("trainer") or {}).get("token")
        if t and secrets.compare_digest(token.encode("utf-8"), t.encode("utf-8")):
            found = Identity("trainer")
        for uid, rec in data["athletes"].items():
            if secrets.compare_digest(token.encode("utf-8"), str(rec.get("token") or "-").encode("utf-8")):
                if (rec.get("expires") or "9999") >= today and str(uid).isdigit():
                    found = Identity("athlete", int(uid), rec.get("expires"), bool(rec.get("chat", True)))
        return found

    # -- the AI allowance of an athlete link -----------------------------------------------------------------
    def use(self, user_id: int, kind: str) -> bool:
        """Count one board / question of today for this person's link. False = allowance used up
        (nothing is counted then)."""
        limit = {"boards": ATHLETE_BOARDS_PER_DAY, "questions": ATHLETE_QUESTIONS_PER_DAY}[kind]
        today = _today({})

        def fn(d):
            rec = d["athletes"].get(str(user_id))
            if not rec:
                return False
            u = self._usage_of(rec, today)
            if u[kind] >= limit:
                return False
            u[kind] += 1
            rec["usage"] = u
            return True
        return self._change(fn)

    def left(self, user_id: int, kind: str) -> int:
        limit = {"boards": ATHLETE_BOARDS_PER_DAY, "questions": ATHLETE_QUESTIONS_PER_DAY}[kind]
        with self._lock:
            rec = self._load()["athletes"].get(str(user_id))
        return max(0, limit - self._usage_of(rec, _today({}))[kind]) if rec else 0


class BadTokens:
    """Wrong tokens per client address: after BAD_TOKEN_MAX within the window the address is
    blocked for a while. The tokens cannot be guessed anyway (192 bit) - this keeps a confused or
    hostile device from hammering the app."""

    def __init__(self):
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}
        self._blocked: dict[str, float] = {}

    def blocked(self, addr: str) -> bool:
        with self._lock:
            until = self._blocked.get(addr)
            if until and until > time.time():
                return True
            self._blocked.pop(addr, None)
            return False

    def fail(self, addr: str) -> None:
        now = time.time()
        with self._lock:
            recent = [t for t in self._fails.get(addr, []) if now - t < BAD_TOKEN_WINDOW_S] + [now]
            self._fails[addr] = recent
            if len(recent) >= BAD_TOKEN_MAX:
                self._blocked[addr] = now + BAD_TOKEN_BLOCK_S
                self._fails.pop(addr, None)
            for a in [a for a, ts in self._fails.items() if now - ts[-1] > BAD_TOKEN_WINDOW_S]:
                self._fails.pop(a, None)               # never grows without bound


class Tickets:
    """One-time download tickets: the page hands the server a finished file and gets a short-lived
    URL back, which the phone's browser can open as a normal download (a script-made download does
    not reach the Files app on an iPhone reliably)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, bytes, str, str]] = {}

    def issue(self, body: bytes, filename: str, ctype: str) -> str:
        now = time.time()
        with self._lock:
            for k in [k for k, v in self._items.items() if now - v[0] > TICKET_TTL_S]:
                self._items.pop(k, None)
            while len(self._items) >= TICKETS_MAX:                    # oldest first
                self._items.pop(min(self._items, key=lambda k: self._items[k][0]), None)
            ticket = secrets.token_urlsafe(24)
            self._items[ticket] = (now, body, filename, ctype)
            return ticket

    def redeem(self, ticket: str) -> tuple[bytes, str, str] | None:
        with self._lock:
            item = self._items.pop(ticket, None)
        if not item or time.time() - item[0] > TICKET_TTL_S:
            return None
        return item[1], item[2], item[3]


def device_of(user_agent: str | None) -> str:
    """A word for the list of connected devices - nothing else of the User-Agent is kept."""
    ua = (user_agent or "").lower()
    for key, label in (("iphone", "iPhone"), ("ipad", "iPad"), ("android", "Android"), ("macintosh", "Mac"),
                       ("windows", "Windows PC"), ("linux", "Linux")):
        if key in ua:
            return label
    return "device"


class Seen:
    """Who used the phone listener lately: {address: (time, role, user_id, device)} - shown in the
    Phone dialog ("iPhone - 3 s ago"), so the owner sees at once whether the QR code worked."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, str, int | None, str]] = {}

    def touch(self, addr: str, who: Identity, user_agent: str | None) -> None:
        now = time.time()
        with self._lock:
            self._items[addr] = (now, who.role, who.user_id, device_of(user_agent))
            for a in [a for a, v in self._items.items() if now - v[0] > SEEN_KEEP_S]:
                self._items.pop(a, None)

    def list(self) -> list[dict]:
        now = time.time()
        with self._lock:
            rows = [{"ago_s": round(now - t), "role": role, "user_id": uid, "device": dev}
                    for t, role, uid, dev in self._items.values() if now - t <= SEEN_KEEP_S]
        return sorted(rows, key=lambda r: r["ago_s"])
