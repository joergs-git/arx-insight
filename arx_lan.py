#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ARX Insight - phone access in the local network (v0.6.0).

OPT-IN and off by default. When the owner switches it on (at the PC), a SECOND listener is bound to
the address of ONE network adapter - never to 0.0.0.0 - so the Windows Firewall prompt appears only
then, and the app is reachable from the same (W)LAN only. Which listener accepted a connection is
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
import os, sys, json, time, base64, socket, ipaddress, threading, subprocess

WATCH_S = 20                       # how often the watchdog looks for a changed address
PORT_TRIES = 6                     # the local port first, then the next ones
PS_TIMEOUT_S = 12
FIREWALL_RULE = "ARX Insight (phone access)"
VIRTUAL_HINTS = ("hyper-v", "vethernet", "virtual", "vmware", "virtualbox", "wsl", "loopback", "bluetooth", "tap-", "tunnel",
                 "vpn", "wireguard", "tailscale", "zerotier", "docker", "npcap", "pseudo")
HERE = os.path.dirname(os.path.abspath(__file__))


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


def ps_encoded(script: str, interactive: bool = False) -> list[str]:
    """Command line for a PowerShell script as -EncodedCommand (base64 of UTF-16LE): no quoting rules
    of cmd / CreateProcess / PowerShell can mangle paths with spaces or quotes on the way."""
    return (["powershell", "-NoProfile"] + ([] if interactive else ["-NonInteractive"])
            + ["-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode("ascii")])


def _powershell(script: str) -> str | None:
    """Output of a short PowerShell script (Windows only), None when it cannot run."""
    if os.name != "nt":
        return None
    try:
        r = subprocess.run(ps_encoded("$ProgressPreference='SilentlyContinue'; " + script), capture_output=True, text=True,
                           timeout=PS_TIMEOUT_S, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


_PS_ADAPTERS = r"""
$rows = Get-NetIPConfiguration | Where-Object { $_.IPv4Address -and $_.NetAdapter.Status -eq 'Up' } | ForEach-Object {
  $p = Get-NetConnectionProfile -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue | Select-Object -First 1
  [pscustomobject]@{ ip = [string]($_.IPv4Address | Select-Object -First 1).IPAddress; name = [string]$_.InterfaceAlias;
    description = [string]$_.InterfaceDescription; gateway = [bool]$_.IPv4DefaultGateway; network = [string]$p.NetworkCategory }
}
ConvertTo-Json -InputObject @($rows) -Compress
"""


def parse_adapters(raw: str | None) -> list[dict]:
    """Rows of _PS_ADAPTERS -> usable adapters: private IPv4, no virtual / VPN adapter, the one
    with a default gateway first. Tolerant: whatever cannot be read is skipped."""
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


_ADAPTERS = {"at": 0.0, "rows": []}


def adapters(fresh: bool = False) -> list[dict]:
    """[{ip, name, gateway, network}] - cached for a few seconds (PowerShell takes about a second)."""
    if not fresh and time.time() - _ADAPTERS["at"] < 10:
        return list(_ADAPTERS["rows"])
    rows = parse_adapters(_powershell(_PS_ADAPTERS))
    ip = primary_ip()
    if ip and ip not in [a["ip"] for a in rows]:                  # not Windows, or PowerShell said nothing
        rows.insert(0, {"ip": ip, "name": "", "gateway": True, "network": None})
    _ADAPTERS.update(at=time.time(), rows=rows)
    return list(rows)


def pick_ip(choice: str | None) -> str | None:
    """'auto' (or nothing) = the adapter with the default route; a fixed address only while this
    machine really has it."""
    if choice and choice != "auto":
        return choice if (private_ipv4(choice) and has_address(choice)) else None
    rows = adapters()
    return rows[0]["ip"] if rows else primary_ip()


class LanManager:
    """The phone listener. make_server(ip, port) -> a started-able server object (arx_app builds it
    with its own Handler, so this module knows nothing about routes)."""

    def __init__(self, make_server, port: int):
        self.make_server, self.port_base = make_server, port
        self._lock = threading.RLock()
        self.server = None
        self.ip, self.port, self.choice, self.error = None, None, "auto", None
        self._watch = None
        self._stop = threading.Event()

    # -- on / off ---------------------------------------------------------------------------------------
    def start(self, choice: str | None = "auto") -> dict:
        with self._lock:
            self._close()
            self.choice = choice or "auto"
            ip = pick_ip(self.choice)
            if not ip:
                self.error = "no_private_address"          # no (W)LAN, or the PC sits on a public address
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
            if self._watch is None:
                self._stop.clear()
                self._watch = threading.Thread(target=self._watchdog, daemon=True)
                self._watch.start()
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
            self._stop.set()
            self._watch = None
            return self.status()

    # -- the address may change (DHCP lease, other Wi-Fi) -----------------------------------------------
    def _watchdog(self) -> None:
        while not self._stop.wait(WATCH_S):
            try:
                with self._lock:
                    if self._stop.is_set():
                        return
                    want = pick_ip(self.choice)
                    if want and want != self.ip:             # new address (or the adapter came back): follow it
                        self.start(self.choice)
                    elif not want and self.server is not None and not has_address(self.ip):
                        self._close()
                        self.error = "address_gone"
            except Exception:
                pass                                         # the watchdog must never die

    def status(self) -> dict:
        with self._lock:
            on = self.server is not None
            return {"running": on, "ip": self.ip, "port": self.port, "choice": self.choice, "error": self.error,
                    "url": f"http://{self.ip}:{self.port}/" if on else None}


# ---- Windows Firewall ------------------------------------------------------------------------------------
def firewall_rule() -> bool | None:
    """Is our inbound rule present? None = not Windows / cannot tell."""
    out = _powershell(f"if (Get-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -ErrorAction SilentlyContinue) {{ 'yes' }} else {{ 'no' }}")
    return None if out is None else out.strip().lower().startswith("yes")


def interpreter_paths() -> list[str]:
    """The Python executables Windows may name in its firewall prompt: the venv's launcher and the
    base interpreter it starts."""
    paths = [sys.executable, getattr(sys, "_base_executable", None)]
    return sorted({os.path.normpath(p) for p in paths if p and os.path.isfile(p)})


def firewall_command(script: str, port: int, remove: bool = False, programs: list[str] | None = None) -> str:
    """The PowerShell line that starts windows/firewall.ps1 ELEVATED (UAC prompt). Every path is a
    single-quoted PowerShell literal (a quote inside is doubled) wrapped in double quotes for the
    new process, so spaces and apostrophes in a user name do no harm."""
    lit = lambda text: "'" + str(text).replace("'", "''") + "'"
    args = ["'-NoProfile'", "'-ExecutionPolicy'", "'Bypass'", "'-File'", lit(f'"{script}"'), "'-Port'", lit(int(port))]
    if remove:
        args.append("'-Remove'")
    if programs:
        args += ["'-Program'", lit('"' + ";".join(programs) + '"')]
    return "Start-Process powershell -Verb RunAs -WindowStyle Hidden -ArgumentList @(" + ",".join(args) + ")"


def firewall_helper(port: int, remove: bool = False) -> bool:
    """Run windows/firewall.ps1 elevated - Windows shows its UAC prompt on the PC's screen. Returns
    whether the prompt could be started at all; the rule itself is checked with firewall_rule()."""
    script = os.path.join(HERE, "windows", "firewall.ps1")
    if os.name != "nt" or not os.path.isfile(script):
        return False
    try:
        subprocess.Popen(ps_encoded(firewall_command(script, port, remove, interpreter_paths()), interactive=True),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except Exception:
        return False
