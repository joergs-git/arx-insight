#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ARX Insight - phone access in the local network (v0.6.0).

ON by default since v0.6.1 (owner's decision; one click at the PC switches it off, and that choice is
kept). A SECOND listener is bound to the address of ONE network adapter - never to 0.0.0.0 - so the
app is reachable from the same (W)LAN only, and on Windows the firewall asks once for it. Which listener accepted a connection is
what decides about trust: the loopback listener is "this machine", the LAN listener always wants
a token (arx_access). Only private IPv4 addresses (10/8, 172.16/12, 192.168/16) are ever bound -
a PC that sits directly on the internet is refused.

  adapters()      what the owner can choose from (Windows: Get-NetIPConfiguration without virtual /
                  VPN adapters; elsewhere: the address the default route uses)
  LanManager      start / stop the listener, follow a changed address (DHCP) in "auto" mode,
                  report the state for the Phone dialog
  firewall_*      Windows only: is our inbound rule there, run the helper script (UAC prompt)

Plain HTTP inside the local network - the README says so, and that the router must never forward
the port. Leaf module apart from arx_base. Public domain / CC0. No warranty.
"""
from __future__ import annotations
import os, sys, json, time, socket, ipaddress, threading, subprocess

WATCH_S = 20                       # how often the watchdog looks for a changed address
PORT_TRIES = 6                     # the local port first, then the next ones
PS_TIMEOUT_S = 12
FIREWALL_RULE = "ARX Insight (phone access)"
VIRTUAL_HINTS = ("hyper-v", "vethernet", "virtual", "vmware", "virtualbox", "wsl", "loopback", "bluetooth", "tap-", "tunnel",
                 "vpn", "wireguard", "tailscale", "zerotier", "docker", "npcap", "pseudo")
HERE = os.path.dirname(os.path.abspath(__file__))
IS_WINDOWS = os.name == "nt"       # one switch for everything Windows-only here (tests flip it together with _powershell)


def private_ipv4(ip: str | None) -> bool:
    """Only addresses of a private network may carry the phone listener."""
    try:
        a = ipaddress.ip_address(str(ip))
    except ValueError:
        return False
    return a.version == 4 and a.is_private and not (a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast)


def primary_ip() -> str | None:
    """The local address the default route uses. A UDP socket is only CONNECTED - no packet is sent
    (192.0.2.1 is a documentation address that never answers)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return ip if private_ipv4(ip) else None


def has_address(ip: str) -> bool:
    """Is this address (still) one of this machine's? Binding a throw-away UDP socket tells."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


def ps_command(script: str, interactive: bool = False) -> list[str]:
    """Command line for a PowerShell script handed over as PLAIN, READABLE TEXT after -Command (v0.8.5).
    Until then the script travelled in PowerShell's encoded form: safe against quoting trouble, but that
    is how malware hides its commands, and Windows security judged the whole release ZIP as a trojan (a
    cloud verdict on the archive, no file named). Quoting cannot go wrong here either, because a script may not contain a double quote: Python
    wraps the argument in double quotes and nothing inside needs escaping - PowerShell literals are
    single-quoted, and where the NEW process needs a double quote it is written as [char]34."""
    if '"' in script:
        raise ValueError("a PowerShell script passed with -Command must not contain a double quote")
    return ["powershell", "-NoProfile"] + ([] if interactive else ["-NonInteractive"]) + ["-Command", script]


def _powershell(script: str) -> str | None:
    """Output of a short PowerShell script (Windows only), None when it cannot run."""
    if not IS_WINDOWS:
        return None
    try:
        r = subprocess.run(ps_command("$ProgressPreference='SilentlyContinue'; " + script), capture_output=True, text=True,
                           timeout=PS_TIMEOUT_S, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


_PS_STATE = r"""
$rows = Get-NetIPConfiguration | Where-Object { $_.IPv4Address -and $_.NetAdapter.Status -eq 'Up' } | ForEach-Object {
  $p = Get-NetConnectionProfile -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue | Select-Object -First 1
  [pscustomobject]@{ ip = [string]($_.IPv4Address | Select-Object -First 1).IPAddress; name = [string]$_.InterfaceAlias;
    description = [string]$_.InterfaceDescription; gateway = [bool]$_.IPv4DefaultGateway; network = [string]$p.NetworkCategory }
}
$rule = [bool](Get-NetFirewallRule -DisplayName '__RULE__' -ErrorAction SilentlyContinue)
ConvertTo-Json -InputObject @{ adapters = @($rows); rule = $rule } -Compress -Depth 4
"""


def parse_adapters(raw) -> list[dict]:
    """Adapter rows of _PS_STATE (JSON text or the parsed list) -> usable adapters: private IPv4, no
    virtual / VPN adapter, the one with a default gateway first. Tolerant: whatever cannot be read
    is skipped."""
    rows = raw
    if isinstance(raw, str) or raw is None:
        try:
            rows = json.loads(raw or "[]")
        except ValueError:
            return []
    if isinstance(rows, dict):
        rows = [rows]
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not private_ipv4(r.get("ip")):
            continue
        label = f"{r.get('name') or ''} {r.get('description') or ''}".lower()
        if any(h in label for h in VIRTUAL_HINTS):
            continue
        out.append({"ip": r["ip"], "name": str(r.get("name") or "")[:60], "gateway": bool(r.get("gateway")),
                    "network": (str(r.get("network") or "") or None)})      # Public | Private | DomainAuthenticated
    out.sort(key=lambda a: (not a["gateway"], a["name"]))
    return out


def parse_state(raw: str | None) -> dict:
    """Output of _PS_STATE -> {adapters, firewall_rule}; firewall_rule None = Windows did not say."""
    try:
        data = json.loads(raw or "")
    except ValueError:
        return {"adapters": [], "firewall_rule": None}
    if not isinstance(data, dict):
        return {"adapters": [], "firewall_rule": None}
    rule = data.get("rule")
    return {"adapters": parse_adapters(data.get("adapters") or []), "firewall_rule": rule if isinstance(rule, bool) else None}


# What Windows knows (adapters without virtual / VPN ones, their network category, our firewall rule) costs a
# PowerShell start: a second at best, ten on a slow kiosk PC. NOTHING a click waits for may depend on it -
# switching phone access on, opening the dialog and the start of the app all use the fast path (primary_ip);
# Windows' answer arrives in the background and only refines the picture (see LanManager.reconsider).
STATE_TTL_S = 10
BACKGROUND_RETRY_S = 300           # Windows said nothing useful: the background path asks again after this long at the earliest
_STATE = {"at": 0.0, "adapters": [], "firewall_rule": None, "known": False, "busy": False}
_STATE_LOCK = threading.Lock()
_STATE_DONE = threading.Event()    # set whenever no question to Windows is under way
_STATE_DONE.set()
_ON_STATE: list = []               # callbacks after a refresh (the LanManager re-checks its address)


def _load_state() -> dict:
    try:
        got = parse_state(_powershell(_PS_STATE.replace("__RULE__", FIREWALL_RULE)))
    except Exception:
        got = {"adapters": [], "firewall_rule": None}
    with _STATE_LOCK:
        _STATE.update(at=time.time(), adapters=got["adapters"], firewall_rule=got["firewall_rule"], known=bool(got["adapters"]), busy=False)
    _STATE_DONE.set()
    for fn in list(_ON_STATE):
        try:
            fn()
        except Exception:
            pass
    return system_info(wait=False, refresh=False)


def system_info(fresh: bool = False, wait: bool = True, refresh: bool = True) -> dict:
    """{adapters, firewall_rule, known}. wait=True asks Windows now when the cache is older than
    STATE_TTL_S (or fresh=True) - only the dialog's background request does that. wait=False never
    blocks: it returns what is cached and (refresh=True) lets a background thread ask Windows when
    nothing is known yet. Not Windows: the address of the default route, nothing else."""
    with _STATE_LOCK:
        age = time.time() - _STATE["at"]
        now = wait and (fresh or age > STATE_TTL_S)                                   # the caller wants Windows' answer
        background = not wait and refresh and not _STATE["known"] and age > BACKGROUND_RETRY_S
        join = IS_WINDOWS and now and _STATE["busy"]                                  # somebody is asking already: wait for that answer
        need = IS_WINDOWS and not _STATE["busy"] and (now or background)
        if need:
            _STATE["busy"] = True
            _STATE_DONE.clear()
    if need and wait:
        _load_state()
    elif need:
        threading.Thread(target=_load_state, daemon=True).start()
    elif join:
        _STATE_DONE.wait(PS_TIMEOUT_S + 3)
    with _STATE_LOCK:
        rows, rule, known = list(_STATE["adapters"]), _STATE["firewall_rule"], _STATE["known"]
    if not rows:                                           # not Windows, or Windows has not answered (yet)
        ip = primary_ip()
        rows = [{"ip": ip, "name": "", "gateway": True, "network": None}] if ip else []
    return {"adapters": rows, "firewall_rule": rule, "known": known}


def adapters(fresh: bool = False) -> list[dict]:
    """[{ip, name, gateway, network}] - asks Windows (blocking) when the cache is stale."""
    return system_info(fresh=fresh)["adapters"]


def pick_ip(choice: str | None) -> str | None:
    """'auto' (or nothing) = the address of the default route - unless Windows has already told us
    that this is a virtual / VPN adapter, then the first real one. A fixed address only while this
    machine really has it. Never blocks (see system_info)."""
    if choice and choice != "auto":
        return choice if (private_ipv4(choice) and has_address(choice)) else None
    info, ip = system_info(wait=False), primary_ip()
    if info["known"]:
        ips = [a["ip"] for a in info["adapters"]]
        return ip if ip in ips else ips[0]
    return ip


class LanManager:
    """The phone listener. make_server(ip, port) -> a started-able server object (arx_app builds it
    with its own Handler, so this module knows nothing about routes)."""

    def __init__(self, make_server, port: int):
        self.make_server, self.port_base = make_server, port
        self._lock = threading.RLock()
        self.server = None
        self.ip, self.port, self.choice, self.error = None, None, "auto", None
        self.wanted = False                # switched on (even while no address is there to listen on)
        self._watch = None
        self._stop = threading.Event()
        _ON_STATE.append(self.reconsider)

    # -- on / off ---------------------------------------------------------------------------------------
    def start(self, choice: str | None = "auto") -> dict:
        with self._lock:
            self._close()
            self.choice = choice or "auto"
            self.wanted = True
            if self._watch is None:                        # started first: it also waits for a network that comes up later
                self._stop = threading.Event()             # its own event: a watchdog that was told to stop never wakes up again
                self._watch = threading.Thread(target=self._watchdog, args=(self._stop,), daemon=True)
                self._watch.start()
            ip = pick_ip(self.choice)
            if not ip:
                self.error = "no_private_address"          # no (W)LAN yet, or the PC sits on a public address
                return self.status()
            for port in range(self.port_base, self.port_base + PORT_TRIES):
                try:
                    srv = self.make_server(ip, port)
                except OSError:
                    continue
                self.server, self.ip, self.port, self.error = srv, ip, port, None
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                break
            else:
                self.error = "no_free_port"
            return self.status()

    def _close(self) -> None:
        srv, self.server = self.server, None
        self.ip = self.port = None
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass

    def stop(self) -> dict:
        with self._lock:
            self._close()
            self.error = None
            self.wanted = False
            self._stop.set()
            self._watch = None
            return self.status()

    # -- the address may change (DHCP lease, other Wi-Fi) -----------------------------------------------
    def _watchdog(self, stop: threading.Event) -> None:
        """Cheap checks only (no PowerShell): our address is gone -> close; no listener but an address
        is there (the Wi-Fi came up after the app, a new DHCP lease) -> start. An address that still
        exists is kept even when the default route moves (a VPN that connects must not cut the phones off)."""
        while not stop.wait(WATCH_S):
            try:
                with self._lock:
                    if stop.is_set() or not self.wanted:
                        return
                    if self.server is not None and not has_address(self.ip):
                        self._close()
                        self.error = "address_gone"
                    if self.server is None and pick_ip(self.choice):
                        self.start(self.choice)
            except Exception:
                pass                                         # the watchdog must never die

    def reconsider(self) -> None:
        """Windows has answered (background): in automatic mode move away from an adapter it calls
        virtual / VPN, to the real one."""
        with self._lock:
            if not self.wanted or self.choice != "auto" or self.server is None:
                return
            want = pick_ip("auto")
            if want and want != self.ip:
                self.start("auto")

    def status(self) -> dict:
        with self._lock:
            on = self.server is not None
            return {"running": on, "wanted": self.wanted, "ip": self.ip, "port": self.port, "choice": self.choice, "error": self.error,
                    "url": f"http://{self.ip}:{self.port}/" if on else None}


# ---- Windows Firewall ------------------------------------------------------------------------------------
def firewall_rule() -> bool | None:
    """Is our inbound rule present? None = not Windows / cannot tell. (Part of the one system_info call.)"""
    return system_info(fresh=True)["firewall_rule"]


def interpreter_paths() -> list[str]:
    """The Python executables Windows may name in its firewall prompt: the venv's launcher and the
    base interpreter it starts."""
    paths = [sys.executable, getattr(sys, "_base_executable", None)]
    return sorted({os.path.normpath(p) for p in paths if p and os.path.isfile(p)})


def firewall_command(script: str, port: int, remove: bool = False, programs: list[str] | None = None) -> str:
    """The PowerShell line that starts windows/firewall.ps1 ELEVATED (UAC prompt). Every path is a
    single-quoted PowerShell literal (a quote inside is doubled); the double quotes the new process
    needs around a path with spaces are put on at run time ([char]34), so the line itself contains
    none and can be passed as plain text (see ps_command)."""
    lit = lambda text: "'" + str(text).replace("'", "''") + "'"
    quoted = lambda text: "('{0}{1}{0}' -f [char]34," + lit(text) + ")"
    args = ["'-NoProfile'", "'-ExecutionPolicy'", "'Bypass'", "'-File'", quoted(script), "'-Port'", lit(int(port))]
    if remove:
        args.append("'-Remove'")
    if programs:
        args += ["'-Program'", quoted(";".join(programs))]
    # a visible window on purpose (v0.8.3): what runs with administrator rights shows itself and its messages
    return "Start-Process powershell -Verb RunAs -ArgumentList @(" + ",".join(args) + ")"


def firewall_helper(port: int, remove: bool = False) -> bool:
    """Run windows/firewall.ps1 elevated - Windows shows its UAC prompt on the PC's screen. Returns
    whether the prompt could be started at all; the rule itself is checked with firewall_rule()."""
    script = os.path.join(HERE, "windows", "firewall.ps1")
    if not IS_WINDOWS or not os.path.isfile(script):
        return False
    try:
        subprocess.Popen(ps_command(firewall_command(script, port, remove, interpreter_paths()), interactive=True),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except Exception:
        return False
