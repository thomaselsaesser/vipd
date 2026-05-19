#!/usr/bin/env python3
"""
vipd - VIP Failover Daemon für Hetzner Cloud Floating IPs

Verwaltet eine oder mehrere Floating IPs zwischen zwei oder mehr Nodes.
- Kein Preemption: Default-Master kommt zurück -> Icinga warnt, VIP bleibt aber
- Lokale Health-Checks (IP am Interface + TCP :443)
- Service-Restart vor Failover (nginx oder traefik)
- Split-Brain-Schutz: Peer-Check über interne UND externe IP
- Peer-Kommunikation: HTTPS mit internem CA-Cert
"""

import asyncio
import logging
import os
import ssl
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("vipd")


# ---------------------------------------------------------------------------
# Konfigurations-Datenklassen
# ---------------------------------------------------------------------------
@dataclass
class PeerConfig:
    name: str
    internal_ip: str
    external_ip: str


@dataclass
class VipConfig:
    name: str
    floating_ip: str
    floating_ip_id: int          # Hetzner Cloud Floating IP ID
    default_master: str          # Hostname des Default-Masters
    interface: str               # z.B. "eth0"
    service: str                 # systemd Service-Name (z.B. "nginx" oder "traefik")
    # HTTP-Health-Check
    check_port: int = 443        # Port für HTTP-Check (lokal über 127.0.0.1)
    check_scheme: str = "https"  # "http" oder "https"
    check_path: str = "/"        # URL-Pfad, z.B. "/healthz"
    check_host_header: str = ""  # Host-Header (leer = kein expliziter Header)
    check_ok_codes: list[int] = field(default_factory=lambda: [200])
    check_expect_string: str = ""  # Optional: Substring der im Body vorkommen muss
    check_timeout_seconds: float = 3.0
    # Service-Recovery
    restart_attempts: int = 1    # Wie oft Service neu starten
    restart_wait_seconds: int = 10  # Wartezeit nach Restart vor Re-Check


@dataclass
class DaemonConfig:
    node_name: str                       # Hostname dieser Node
    listen_address: str = "0.0.0.0"
    listen_port: int = 8443
    check_interval_seconds: int = 5      # Wie oft die Health-Loop läuft
    peer_timeout_seconds: float = 3.0    # Timeout für Peer-HTTPS-Calls
    secrets_file: str = "/etc/vipd/secrets"
    cert_file: str = "/etc/vipd/cert.pem"
    key_file: str = "/etc/vipd/key.pem"
    ca_file: str = "/etc/vipd/ca.pem"
    auth_token: str = ""                 # Wird aus secrets-File geladen
    hetzner_token: str = ""              # Wird aus secrets-File geladen
    peers: list[PeerConfig] = field(default_factory=list)
    vips: list[VipConfig] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Globaler State
# ---------------------------------------------------------------------------
@dataclass
class VipState:
    """Laufzeit-Status pro VIP auf dieser Node."""
    holding: bool = False              # Halten wir die VIP gerade?
    last_local_check_ok: bool = False  # Letzter lokaler Health-Check ok?
    last_check_timestamp: float = 0.0
    last_error: str = ""
    service_restart_in_progress: bool = False


CONFIG: Optional[DaemonConfig] = None
VIP_STATES: dict[str, VipState] = {}  # Key: VIP-Name


# ---------------------------------------------------------------------------
# Konfiguration laden
# ---------------------------------------------------------------------------
def load_config(path: str) -> DaemonConfig:
    with open(path, "r") as f:
        data = yaml.safe_load(f)

    cfg = DaemonConfig(
        node_name=data["node_name"],
        listen_address=data.get("listen_address", "0.0.0.0"),
        listen_port=data.get("listen_port", 8443),
        check_interval_seconds=data.get("check_interval_seconds", 5),
        peer_timeout_seconds=data.get("peer_timeout_seconds", 3.0),
        secrets_file=data.get("secrets_file", "/etc/vipd/secrets"),
        cert_file=data.get("cert_file", "/etc/vipd/cert.pem"),
        key_file=data.get("key_file", "/etc/vipd/key.pem"),
        ca_file=data.get("ca_file", "/etc/vipd/ca.pem"),
    )

    for p in data.get("peers", []):
        cfg.peers.append(PeerConfig(
            name=p["name"],
            internal_ip=p["internal_ip"],
            external_ip=p["external_ip"],
        ))

    for v in data.get("vips", []):
        cfg.vips.append(VipConfig(
            name=v["name"],
            floating_ip=v["floating_ip"],
            floating_ip_id=v["floating_ip_id"],
            default_master=v["default_master"],
            interface=v.get("interface", "eth0"),
            service=v["service"],
            check_port=v.get("check_port", 443),
            check_scheme=v.get("check_scheme", "https"),
            check_path=v.get("check_path", "/"),
            check_host_header=v.get("check_host_header", ""),
            check_ok_codes=v.get("check_ok_codes", [200]),
            check_expect_string=v.get("check_expect_string", ""),
            check_timeout_seconds=v.get("check_timeout_seconds", 3.0),
            restart_attempts=v.get("restart_attempts", 1),
            restart_wait_seconds=v.get("restart_wait_seconds", 10),
        ))

    # Secrets laden (separate Datei, Mode 0600)
    load_secrets(cfg)
    return cfg


def load_secrets(cfg: DaemonConfig) -> None:
    """Lädt Hetzner Token und Auth Token aus separater Secrets-Datei."""
    secrets_path = Path(cfg.secrets_file)
    if not secrets_path.exists():
        raise FileNotFoundError(f"Secrets-Datei nicht gefunden: {cfg.secrets_file}")

    # Mode-Check (muss 0600 sein)
    mode = secrets_path.stat().st_mode & 0o777
    if mode != 0o600:
        log.warning(
            "Secrets-Datei %s hat Mode %o, erwartet 0600", cfg.secrets_file, mode
        )

    with open(secrets_path, "r") as f:
        secrets = yaml.safe_load(f)

    cfg.hetzner_token = secrets["hetzner_token"]
    cfg.auth_token = secrets["auth_token"]


# ---------------------------------------------------------------------------
# Lokale Health-Checks
# ---------------------------------------------------------------------------
def ip_on_interface(ip: str, interface: str) -> bool:
    """Prüft, ob die angegebene IP am Interface hängt."""
    try:
        result = subprocess.run(
            ["ip", "-4", "addr", "show", "dev", interface],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return ip in result.stdout
    except (subprocess.SubprocessError, OSError) as e:
        log.error("ip addr show fehlgeschlagen: %s", e)
        return False


async def http_check(vip: VipConfig) -> tuple[bool, str]:
    """HTTP-Health-Check gegen 127.0.0.1:<check_port>.

    Erfolg = Status-Code in check_ok_codes UND (falls gesetzt) expect_string im Body.
    Returns: (ok, reason_if_not_ok)
    """
    url = f"{vip.check_scheme}://127.0.0.1:{vip.check_port}{vip.check_path}"
    headers = {}
    if vip.check_host_header:
        headers["Host"] = vip.check_host_header

    try:
        # verify=False: lokaler Check, Cert ist auf externen Hostnamen ausgestellt
        async with httpx.AsyncClient(
            verify=False,
            timeout=vip.check_timeout_seconds,
            follow_redirects=False,
        ) as client:
            r = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        return False, f"HTTP-Request fehlgeschlagen: {e.__class__.__name__}: {e}"

    if r.status_code not in vip.check_ok_codes:
        return False, (
            f"Status {r.status_code} nicht in erlaubten Codes {vip.check_ok_codes}"
        )

    if vip.check_expect_string:
        if vip.check_expect_string not in r.text:
            return False, (
                f"expect_string '{vip.check_expect_string}' nicht im Response-Body"
            )

    return True, ""


def restart_service(service: str) -> bool:
    """systemctl restart <service>."""
    try:
        result = subprocess.run(
            ["systemctl", "restart", service],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode == 0:
            log.info("Service %s neu gestartet", service)
            return True
        log.error("systemctl restart %s fehlgeschlagen: %s", service, result.stderr)
        return False
    except (subprocess.SubprocessError, OSError) as e:
        log.error("systemctl restart %s exception: %s", service, e)
        return False


# ---------------------------------------------------------------------------
# Hetzner API + lokales Interface-Handling
# ---------------------------------------------------------------------------
async def hetzner_assign_floating_ip(vip: VipConfig, server_id: int) -> bool:
    """Hängt die Floating IP auf den Server mit der gegebenen ID um."""
    url = f"https://api.hetzner.cloud/v1/floating_ips/{vip.floating_ip_id}/actions/assign"
    headers = {
        "Authorization": f"Bearer {CONFIG.hetzner_token}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, headers=headers, json={"server": server_id})
            if r.status_code in (200, 201):
                log.info("Hetzner API: Floating IP %s -> Server %d ok",
                         vip.floating_ip, server_id)
                return True
            log.error("Hetzner API Fehler %d: %s", r.status_code, r.text)
            return False
    except httpx.HTTPError as e:
        log.error("Hetzner API Exception: %s", e)
        return False


def get_own_server_id() -> Optional[int]:
    """Holt die eigene Hetzner Server-ID aus den Metadaten."""
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "3",
             "http://169.254.169.254/hetzner/v1/metadata/instance-id"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return int(result.stdout.strip())
    except (subprocess.SubprocessError, ValueError, OSError) as e:
        log.error("Konnte eigene Server-ID nicht ermitteln: %s", e)
        return None


def add_ip_to_interface(ip: str, interface: str) -> bool:
    """ip addr add <ip>/32 dev <interface>."""
    try:
        result = subprocess.run(
            ["ip", "addr", "add", f"{ip}/32", "dev", interface],
            capture_output=True, text=True, timeout=5, check=False,
        )
        # "File exists" ist ok (idempotent)
        if result.returncode == 0 or "File exists" in result.stderr:
            return True
        log.error("ip addr add fehlgeschlagen: %s", result.stderr)
        return False
    except (subprocess.SubprocessError, OSError) as e:
        log.error("ip addr add exception: %s", e)
        return False


def del_ip_from_interface(ip: str, interface: str) -> bool:
    """ip addr del <ip>/32 dev <interface>."""
    try:
        result = subprocess.run(
            ["ip", "addr", "del", f"{ip}/32", "dev", interface],
            capture_output=True, text=True, timeout=5, check=False,
        )
        # "Cannot assign requested address" ist ok (war nicht da)
        if result.returncode == 0 or "Cannot assign" in result.stderr:
            return True
        log.error("ip addr del fehlgeschlagen: %s", result.stderr)
        return False
    except (subprocess.SubprocessError, OSError) as e:
        log.error("ip addr del exception: %s", e)
        return False


async def take_over_vip(vip: VipConfig) -> bool:
    """VIP übernehmen: Hetzner API + lokales Interface."""
    server_id = get_own_server_id()
    if server_id is None:
        return False

    if not await hetzner_assign_floating_ip(vip, server_id):
        return False

    if not add_ip_to_interface(vip.floating_ip, vip.interface):
        return False

    VIP_STATES[vip.name].holding = True
    log.info("VIP %s (%s) übernommen", vip.name, vip.floating_ip)
    return True


def release_vip(vip: VipConfig) -> None:
    """VIP lokal vom Interface entfernen. (Hetzner-seitig bleibt sie zugewiesen,
    bis ein anderer Node sie übernimmt – das ist Hetzner-Logik.)"""
    del_ip_from_interface(vip.floating_ip, vip.interface)
    VIP_STATES[vip.name].holding = False
    log.info("VIP %s (%s) freigegeben", vip.name, vip.floating_ip)


# ---------------------------------------------------------------------------
# Peer-Kommunikation (HTTPS Client)
# ---------------------------------------------------------------------------
def make_ssl_context() -> ssl.SSLContext:
    """SSL-Context für ausgehende Peer-Calls (verifiziert gegen internes CA)."""
    ctx = ssl.create_default_context(cafile=CONFIG.ca_file)
    return ctx


async def peer_get_status(peer_ip: str) -> Optional[dict]:
    """Holt /status von einem Peer."""
    url = f"https://{peer_ip}:{CONFIG.listen_port}/status"
    headers = {"Authorization": f"Bearer {CONFIG.auth_token}"}
    try:
        async with httpx.AsyncClient(
            verify=CONFIG.ca_file,
            timeout=CONFIG.peer_timeout_seconds,
        ) as client:
            r = await client.get(url, headers=headers)
            if r.status_code == 200:
                return r.json()
            return None
    except httpx.HTTPError:
        return None


async def peer_is_alive(peer: PeerConfig) -> tuple[bool, bool]:
    """Prüft Peer-Erreichbarkeit über interne UND externe IP.
    
    Returns: (internal_ok, external_ok)
    """
    internal_ok = (await peer_get_status(peer.internal_ip)) is not None
    external_ok = (await peer_get_status(peer.external_ip)) is not None
    return internal_ok, external_ok


def find_peer_by_name(name: str) -> Optional[PeerConfig]:
    for p in CONFIG.peers:
        if p.name == name:
            return p
    return None


async def peer_holds_vip(peer: PeerConfig, vip_name: str) -> Optional[bool]:
    """Fragt einen Peer, ob er eine bestimmte VIP hält.
    Returns None wenn Peer nicht erreichbar."""
    status = await peer_get_status(peer.internal_ip)
    if status is None:
        status = await peer_get_status(peer.external_ip)
    if status is None:
        return None
    for v in status.get("vips", []):
        if v["name"] == vip_name:
            return v["holding"]
    return False


# ---------------------------------------------------------------------------
# Health-Check + Entscheidungs-Loop
# ---------------------------------------------------------------------------
async def local_health_ok(vip: VipConfig) -> tuple[bool, str]:
    """Lokaler Health-Check: IP am Interface + HTTP-Check.
    Returns: (ok, reason_if_not_ok)
    """
    if not ip_on_interface(vip.floating_ip, vip.interface):
        return False, f"VIP {vip.floating_ip} nicht an {vip.interface}"
    return await http_check(vip)


async def try_service_recovery(vip: VipConfig) -> bool:
    """Versucht, den Service neu zu starten und prüft danach.
    Returns True wenn nach Restart wieder ok."""
    state = VIP_STATES[vip.name]
    state.service_restart_in_progress = True
    try:
        for attempt in range(1, vip.restart_attempts + 1):
            log.warning("VIP %s: Service %s Restart-Versuch %d/%d",
                        vip.name, vip.service, attempt, vip.restart_attempts)
            if not restart_service(vip.service):
                continue
            await asyncio.sleep(vip.restart_wait_seconds)
            ok, _reason = await local_health_ok(vip)
            if ok:
                log.info("VIP %s: Service-Recovery erfolgreich", vip.name)
                return True
        log.error("VIP %s: Service-Recovery fehlgeschlagen nach %d Versuchen",
                  vip.name, vip.restart_attempts)
        return False
    finally:
        state.service_restart_in_progress = False


async def handle_vip_as_master(vip: VipConfig) -> None:
    """Entscheidungslogik wenn wir die VIP halten."""
    state = VIP_STATES[vip.name]

    # Prüfen, ob IP wirklich am Interface ist; falls nicht, wieder zuweisen
    if not ip_on_interface(vip.floating_ip, vip.interface):
        log.warning("VIP %s sollte hier hängen, ist aber weg – neu zuweisen",
                    vip.name)
        add_ip_to_interface(vip.floating_ip, vip.interface)

    ok, reason = await local_health_ok(vip)
    state.last_local_check_ok = ok
    state.last_error = reason

    if ok:
        return

    # Service kaputt -> Recovery versuchen
    log.warning("VIP %s lokal nicht ok: %s", vip.name, reason)
    if await try_service_recovery(vip):
        state.last_local_check_ok = True
        state.last_error = ""
        return

    # Recovery fehlgeschlagen -> IP abgeben, damit Peer übernehmen kann
    log.error("VIP %s: gebe IP ab, damit ein Peer übernehmen kann", vip.name)
    release_vip(vip)


async def handle_vip_as_backup(vip: VipConfig) -> None:
    """Entscheidungslogik wenn wir die VIP NICHT halten."""
    # Fall 1: Wir sind Default-Master und die VIP läuft woanders -> nichts tun
    #         (kein Preemption). Icinga warnt.
    # Fall 2: Wir sind Backup -> prüfen ob Master noch lebt.

    # Wer hält die VIP gerade laut Peers?
    holder_found = False
    for peer in CONFIG.peers:
        holds = await peer_holds_vip(peer, vip.name)
        if holds is True:
            holder_found = True
            break

    if holder_found:
        return  # Jemand hält sie, alles gut

    # Niemand hält sie. Prüfen: Lebt der Default-Master?
    default_master_peer = find_peer_by_name(vip.default_master)

    if vip.default_master == CONFIG.node_name:
        # Wir SIND der Default-Master, aber wir halten die VIP nicht.
        # Das ist der "Cold Start" oder "nach Crash"-Fall: übernehmen.
        log.info("VIP %s: wir sind Default-Master und niemand hält sie -> übernehmen",
                 vip.name)
        await take_over_vip(vip)
        return

    # Wir sind Backup. Default-Master fragen.
    if default_master_peer is not None:
        internal_ok, external_ok = await peer_is_alive(default_master_peer)
        if internal_ok or external_ok:
            # Master lebt, hält die VIP aber gerade nicht – kurzer Übergang,
            # ein Zyklus warten
            log.info("VIP %s: Default-Master %s lebt, hält VIP aber nicht – warte",
                     vip.name, vip.default_master)
            return
        # Master tot über beide Pfade -> übernehmen
        log.warning("VIP %s: Default-Master %s über beide Pfade tot -> übernehmen",
                    vip.name, vip.default_master)
        await take_over_vip(vip)
        return

    # Default-Master ist gar kein bekannter Peer (Config-Fehler?)
    log.error("VIP %s: Default-Master '%s' nicht in Peers gefunden",
              vip.name, vip.default_master)


async def health_loop() -> None:
    """Haupt-Health-Loop, läuft endlos."""
    log.info("Health-Loop gestartet (Intervall: %ds)",
             CONFIG.check_interval_seconds)
    while True:
        try:
            for vip in CONFIG.vips:
                state = VIP_STATES[vip.name]
                state.last_check_timestamp = time.time()

                if state.holding:
                    await handle_vip_as_master(vip)
                else:
                    await handle_vip_as_backup(vip)
        except Exception as e:
            log.exception("Fehler in Health-Loop: %s", e)

        await asyncio.sleep(CONFIG.check_interval_seconds)


# ---------------------------------------------------------------------------
# FastAPI: Peer-API + Status für Icinga
# ---------------------------------------------------------------------------
security = HTTPBearer()


def verify_auth(creds: HTTPAuthorizationCredentials = Depends(security)) -> None:
    if creds.credentials != CONFIG.auth_token:
        raise HTTPException(status_code=401, detail="invalid token")


class VipStatusResponse(BaseModel):
    name: str
    floating_ip: str
    default_master: str
    holding: bool
    local_check_ok: bool
    is_default_master: bool
    last_error: str
    last_check_age_seconds: float


class StatusResponse(BaseModel):
    node: str
    vips: list[VipStatusResponse]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startet die Health-Loop beim API-Start."""
    task = asyncio.create_task(health_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/status", response_model=StatusResponse)
async def get_status(_: None = Depends(verify_auth)) -> StatusResponse:
    """Status-Endpoint für Peers und Icinga."""
    now = time.time()
    vips = []
    for v in CONFIG.vips:
        s = VIP_STATES[v.name]
        vips.append(VipStatusResponse(
            name=v.name,
            floating_ip=v.floating_ip,
            default_master=v.default_master,
            holding=s.holding,
            local_check_ok=s.last_local_check_ok,
            is_default_master=(v.default_master == CONFIG.node_name),
            last_error=s.last_error,
            last_check_age_seconds=now - s.last_check_timestamp
            if s.last_check_timestamp > 0 else -1.0,
        ))
    return StatusResponse(node=CONFIG.node_name, vips=vips)


@app.get("/healthz")
async def healthz() -> dict:
    """Einfacher Liveness-Check (ohne Auth)."""
    return {"status": "ok", "node": CONFIG.node_name}


def _find_vip(vip_name: str) -> VipConfig:
    for v in CONFIG.vips:
        if v.name == vip_name:
            return v
    raise HTTPException(status_code=404, detail=f"VIP '{vip_name}' nicht in Config")


@app.post("/vips/{vip_name}/release")
async def api_release(vip_name: str, _: None = Depends(verify_auth)) -> dict:
    """Gibt die VIP lokal frei (entfernt sie vom Interface).
    Damit kann ein Peer sie übernehmen. Nutzt vipctl für kontrolliertes Failover.
    """
    vip = _find_vip(vip_name)
    if not VIP_STATES[vip.name].holding:
        return {"ok": True, "action": "noop", "reason": "halte VIP gar nicht"}
    release_vip(vip)
    return {"ok": True, "action": "released"}


@app.post("/vips/{vip_name}/takeover")
async def api_takeover(vip_name: str, _: None = Depends(verify_auth)) -> dict:
    """Erzwingt Übernahme der VIP auf diese Node, unabhängig vom Default-Master.
    Hetzner API entscheidet, ob es klappt – sie kann nur an einem Server hängen.
    """
    vip = _find_vip(vip_name)
    if VIP_STATES[vip.name].holding:
        return {"ok": True, "action": "noop", "reason": "halte VIP bereits"}
    success = await take_over_vip(vip)
    return {"ok": success, "action": "takeover" if success else "failed"}


@app.post("/vips/{vip_name}/check")
async def api_check(vip_name: str, _: None = Depends(verify_auth)) -> dict:
    """Führt sofort einen Health-Check für die VIP aus und gibt das Detail-Ergebnis zurück.
    Zum Debuggen / vipctl check.
    """
    vip = _find_vip(vip_name)
    has_ip = ip_on_interface(vip.floating_ip, vip.interface)
    http_ok, http_reason = await http_check(vip)
    return {
        "vip": vip.name,
        "floating_ip": vip.floating_ip,
        "ip_on_interface": has_ip,
        "http_ok": http_ok,
        "http_reason": http_reason,
        "check_url": f"{vip.check_scheme}://127.0.0.1:{vip.check_port}{vip.check_path}",
        "check_host_header": vip.check_host_header,
        "check_ok_codes": vip.check_ok_codes,
        "check_expect_string": vip.check_expect_string,
        "overall_ok": has_ip and http_ok,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    global CONFIG
    config_path = os.environ.get("VIPD_CONFIG", "/etc/vipd/vipd.yaml")
    log.info("Lade Config von %s", config_path)
    CONFIG = load_config(config_path)

    log.info("Node: %s, %d VIPs, %d Peers",
             CONFIG.node_name, len(CONFIG.vips), len(CONFIG.peers))

    # State initialisieren
    for v in CONFIG.vips:
        VIP_STATES[v.name] = VipState()
        # Beim Start prüfen, ob wir die IP gerade halten
        if ip_on_interface(v.floating_ip, v.interface):
            VIP_STATES[v.name].holding = True
            log.info("VIP %s ist beim Start bereits an %s -> wir halten sie",
                     v.name, v.interface)

    uvicorn.run(
        app,
        host=CONFIG.listen_address,
        port=CONFIG.listen_port,
        ssl_certfile=CONFIG.cert_file,
        ssl_keyfile=CONFIG.key_file,
        log_config=None,  # Wir haben unser eigenes Logging
    )


if __name__ == "__main__":
    main()
