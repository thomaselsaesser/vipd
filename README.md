# vipd – VIP Failover Daemon

Verwaltet Hetzner Cloud Floating IPs zwischen zwei oder mehr Nodes
ohne keepalived/VRRP (das bei Hetzner nicht funktioniert). Steuert die
Floating-IP-Zuweisung über die Hetzner Cloud API und legt die IP zusätzlich
lokal aufs Interface.

## Eigenschaften

- **Pro VIP konfigurierbarer Default-Master** – jeder VIP hat genau eine
  "Heimat-Node", die ihn standardmäßig hält.
- **Kein Preemption** – wenn ein Default-Master nach Ausfall wieder kommt,
  bleibt die VIP beim aktuellen Halter.
- **Lokale Health-Checks** – IP am Interface + HTTP-Check (Status-Code-Liste, optional
  Substring-Match im Body, optional Host-Header).
- **Service-Recovery vor Failover** – nginx/traefik wird erst neu gestartet
  (konfigurierbar oft), bevor die VIP abgegeben wird.
- **Split-Brain-Schutz** – ein Backup übernimmt nur, wenn der Master sowohl
  über interne als auch externe IP nicht erreichbar ist.
- **Peer-Kommunikation über HTTPS** mit internem CA-Cert + Bearer-Token.
- **Mehrere VIPs gleichzeitig** mit verschiedenen Default-Mastern (Lastverteilung).
- **Status-Endpoint für Icinga2** – passive Beobachtung, kein aktives Failover
  durch Icinga.

## Architektur

```
                      ┌────────────────────┐
                      │  Hetzner Cloud API │
                      └─────────▲──────────┘
                                │ assign Floating IP
                                │
        ┌───────────────────────┴────────────────────────┐
        │                                                │
        │   Floating IP 195.201.251.226                  │
        │   (Default-Master: lb01)                       │
        ▼                                                ▼
  ┌──────────┐   HTTPS Peer-API (Port 8443)    ┌──────────┐
  │   lb01   │◄────────────── 10.1.0.0/24 ────►│   lb02   │
  │  vipd    │                                  │  vipd    │
  │  nginx   │                                  │  nginx   │
  └────▲─────┘                                  └────▲─────┘
       │ /status                                     │ /status
       │                                              │
       │         ┌────────────────────┐               │
       └─────────│  Icinga2 (extern)  │───────────────┘
                 │  check_vipd.py     │
                 └────────────────────┘
```

## Voraussetzungen

- RHEL 9, Debian 12 oder Ubuntu 22.04+ (Python 3.9+)
- Root-Rechte (für `ip addr add/del`)
- Hetzner Cloud API Token mit Read+Write
- Internes CA-Cert + Server-Zertifikat pro Node
- Backend-Netz zwischen den Nodes (z.B. 10.1.0.0/24)

## Dateien

| Pfad                          | Inhalt                                       |
|-------------------------------|----------------------------------------------|
| `/opt/vipd/vipd.py`           | Hauptdaemon                                  |
| `/opt/vipd/venv/`             | Python virtualenv mit Requirements           |
| `/etc/vipd/vipd.yaml`         | Konfiguration (pro Node unterschiedlich)     |
| `/etc/vipd/secrets`           | Hetzner Token + Auth Token (Mode 0600)       |
| `/etc/vipd/cert.pem`          | Server-Zertifikat dieser Node                |
| `/etc/vipd/key.pem`           | Private Key (Mode 0600)                      |
| `/etc/vipd/ca.pem`            | Internes CA-Zertifikat                       |
| `/etc/systemd/system/vipd.service` | systemd Unit                            |

## Manuelles Setup (ohne Pipeline)

```bash
# Verzeichnisse
mkdir -p /opt/vipd /etc/vipd
apt install python3.12-venv

# Code + virtualenv
cp vipd.py /opt/vipd/
cp requirements.txt /opt/vipd/
cd /opt/vipd
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cd
# Config + Secrets
cp vipd.yaml.example /etc/vipd/vipd.yaml
# vipd.yaml editieren: node_name, peers, vips anpassen
cp secrets.example /etc/vipd/secrets
chmod 600 /etc/vipd/secrets
# secrets editieren: hetzner_token und auth_token eintragen

# TLS-Zertifikate (Beispiel: interner CA Wildcard)
cp /pfad/zum/internen-ca.pem /etc/vipd/ca.pem
cp /pfad/zum/server-cert.pem /etc/vipd/cert.pem
cp /pfad/zum/server-key.pem  /etc/vipd/key.pem
chmod 600 /etc/vipd/key.pem

mkdir -p /opt/vipd /etc/vipd
# Dateien vom Repo nach /opt/vipd kopieren:
#   vipd.py, vipctl, requirements.txt
cd /opt/vipd
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
2. Wrapper für vipctl
cat > /usr/local/bin/vipctl <<'EOF'
#!/bin/sh
exec /opt/vipd/venv/bin/python /opt/vipd/vipctl "$@"
EOF
chmod +x /usr/local/bin/vipctl



# systemd
cp vipd.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vipd
systemctl status vipd
journalctl -u vipd -f
```

## Konfiguration

Siehe `etc/vipd.yaml.example` mit Inline-Kommentaren.

**Wichtig:** Die `vips:`-Liste muss auf **allen Peers** identisch sein.
Nur `node_name` und `peers:` unterscheiden sich pro Host.

### Beispiel: Lastverteilung mit zwei VIPs

```yaml
# auf lb01:
node_name: lb01
peers:
  - { name: lb02, internal_ip: 10.1.0.12, external_ip: 195.201.10.12 }
vips:
  - { name: web-vip, floating_ip: ..., default_master: lb01, ... }
  - { name: api-vip, floating_ip: ..., default_master: lb02, ... }

# auf lb02:
node_name: lb02
peers:
  - { name: lb01, internal_ip: 10.1.0.11, external_ip: 195.201.10.11 }
vips:
  - { name: web-vip, floating_ip: ..., default_master: lb01, ... }
  - { name: api-vip, floating_ip: ..., default_master: lb02, ... }
```

Im Normalbetrieb hält lb01 die web-vip, lb02 die api-vip. Fällt einer aus,
übernimmt der andere beide.

## vipctl – CLI

Wird im Repo unter `vipctl` ausgeliefert, deployed nach `/usr/local/bin/vipctl`.
Liest dieselbe `/etc/vipd/vipd.yaml` wie der Daemon und kennt damit alle Peers.

```bash
# Übersicht: Status aller Nodes + aller VIPs
vipctl status

# Beispiel-Output:
#
# Node         Status
# ------------------------
# lb01         OK
# lb02         OK
#
# VIP               Floating IP         Default    Halter     Health
# -------------------------------------------------------------------
# web-vip           195.201.251.226     lb01       lb01       OK
# api-vip           195.201.251.227     lb02       lb01       OK         *
#
# * = VIP läuft nicht auf Default-Master

# JSON-Output für Skripte
vipctl status --json

# Manueller Health-Check (zeigt: IP am Interface? HTTP-Code? expect_string?)
vipctl check web-vip
vipctl check web-vip --node lb02       # auf Peer ausführen

# VIP lokal freigeben (Peer übernimmt im nächsten Zyklus)
vipctl release web-vip
vipctl release web-vip --node lb02

# VIP zwingend übernehmen
vipctl takeover web-vip                # auf lokaler Node
vipctl takeover web-vip --node lb01    # lb01 soll übernehmen

# Geordnetes Failback nach Master-Reparatur:
# Findet aktuellen Halter, lässt ihn freigeben, Default-Master übernimmt automatisch
vipctl failover web-vip

# Explizites Ziel statt Default-Master
vipctl failover web-vip --to lb02
```

`vipctl` kommuniziert über die gleiche HTTPS-API wie der Daemon untereinander.
Bei `--node <name>` wird zuerst über die interne IP, dann über die externe IP
versucht. Lokale Calls gehen über `https://127.0.0.1:8443` mit deaktivierter
TLS-Verifizierung (Cert läuft auf externen Hostnamen).

## Verhalten / Entscheidungslogik

### Auf der Node, die eine VIP hält (Master für diese VIP)

Alle `check_interval_seconds` (default 5s):

1. Prüfe: Hängt die IP wirklich am Interface? Falls nein → neu hinzufügen.
2. HTTP-Check gegen `{scheme}://127.0.0.1:{check_port}{check_path}`:
   - Status-Code muss in `check_ok_codes` sein
   - Falls `check_expect_string` gesetzt: muss als Substring im Body vorkommen
   - Host-Header wird mitgeschickt, falls konfiguriert
3. Wenn nicht ok:
   - `systemctl restart <service>` (bis zu `restart_attempts` mal)
   - Jeweils `restart_wait_seconds` warten und nochmal prüfen
4. Wenn immer noch nicht ok → IP lokal abgeben (Peer übernimmt im nächsten Zyklus).

### Auf einer Node, die eine VIP NICHT hält

1. Frage alle Peers: Hält jemand diese VIP?
2. Ja → nichts tun.
3. Nein, und wir SIND der Default-Master → übernehmen (Cold Start / nach Crash).
4. Nein, und wir sind Backup:
   - Default-Master über interne IP erreichbar? → warten
   - Über externe IP erreichbar? → warten
   - Beide tot → übernehmen.

### Kein Preemption

Wenn lb01 (Default-Master für web-vip) ausfällt und lb02 übernimmt,
dann lb01 zurückkommt: lb02 behält die VIP. Icinga warnt mit
"web-vip läuft auf lb02, ist aber NICHT Default-Master". Zurückschalten
mit dem CLI-Tool:

```bash
# Auf einer beliebigen Node:
vipctl failover web-vip
```

Das macht intern: aktueller Halter wird gefunden → Release-Call → Default-Master
übernimmt automatisch beim nächsten Health-Loop.

## API-Endpoints

Alle Endpoints lauschen auf `https://<node>:8443`.

### `GET /healthz` (ohne Auth)

Liveness-Check.

```json
{"status": "ok", "node": "lb01"}
```

### `GET /status` (Bearer Token nötig)

Vollständiger Status für Peers und Icinga.

```json
{
  "node": "lb01",
  "vips": [
    {
      "name": "web-vip",
      "floating_ip": "195.201.251.226",
      "default_master": "lb01",
      "holding": true,
      "local_check_ok": true,
      "is_default_master": true,
      "last_error": "",
      "last_check_age_seconds": 2.3
    }
  ]
}
```

### `POST /vips/{vip_name}/release` (Bearer Token nötig)

Gibt die VIP lokal frei (entfernt sie vom Interface). Ein Peer übernimmt im
nächsten Health-Loop-Zyklus. Wenn die Node die VIP nicht hält, ist das ein No-Op.

### `POST /vips/{vip_name}/takeover` (Bearer Token nötig)

Erzwingt Übernahme: Hetzner-API-Call + lokales Interface-Add. Hetzner entscheidet,
ob die Zuweisung klappt (Floating IP kann nur an einem Server hängen).

### `POST /vips/{vip_name}/check` (Bearer Token nötig)

Führt sofort einen Health-Check aus und liefert Detail-Output. Zum Debuggen.

```json
{
  "vip": "web-vip",
  "floating_ip": "195.201.251.226",
  "ip_on_interface": true,
  "http_ok": false,
  "http_reason": "Status 503 nicht in erlaubten Codes [200]",
  "check_url": "https://127.0.0.1:443/healthz",
  "check_host_header": "www.hlnug.de",
  "check_ok_codes": [200],
  "check_expect_string": "OK",
  "overall_ok": false
}
```

## Icinga2-Integration

Siehe `icinga/check_vipd.py` und `icinga/icinga2-vipd.conf`.

```
OK       - Alle VIPs auf ihrem Default-Master, lokal ok
WARNING  - VIP auf "falscher" Node (Failover-Zustand), Default-Master sollte
           geprüft werden; oder lokaler Health-Check fehlgeschlagen
CRITICAL - vipd nicht erreichbar (Host- oder Service-Ausfall)
UNKNOWN  - Antwort nicht parsebar, VIP nicht in Status
```

## GitLab Pipeline

Siehe `gitlab/.gitlab-ci.yml`. CI/CD-Variablen pro Umgebung:

| Variable                | Inhalt                          |
|-------------------------|--------------------------------|
| `VIPD_SSH_KEY`          | SSH Private Key (Deploy-User)   |
| `HETZNER_TOKEN_HLNUG`   | Hetzner API Token HLNUG         |
| `AUTH_TOKEN_HLNUG`      | Peer Shared Secret HLNUG        |
| `HETZNER_TOKEN_ANIMATE` | Hetzner API Token animate       |
| `AUTH_TOKEN_ANIMATE`    | Peer Shared Secret animate      |

`auth_token` pro Umgebung generieren:
```bash
openssl rand -hex 32
```

## Bekannte Einschränkungen

- **Hetzner-API-Latenz**: Eine Floating-IP-Umhängung dauert typischerweise
  1–5 Sekunden. In der Zeit ist die VIP nicht erreichbar.
- **Keine Quorum-Logik**: Bei zwei Nodes und vollständigem Backend-Netz-Ausfall
  fallen wir auf den externen Pfad zurück. Bei drei oder mehr Nodes könnte
  echtes Quorum-Voting sinnvoll sein – aktuell nicht implementiert.
- **Service-Erkennung**: Wir prüfen nur TCP `:port`. Wenn nginx läuft, aber
  500er liefert, merken wir das nicht. Bei Bedarf einen HTTP-Check ergänzen.

## Troubleshooting

```bash
# Live-Logs
journalctl -u vipd -f

# Aktueller Status (lokal)
curl -k -H "Authorization: Bearer $AUTH_TOKEN" https://localhost:8443/status | jq

# Peer testen
curl --cacert /etc/vipd/ca.pem \
     -H "Authorization: Bearer $AUTH_TOKEN" \
     https://10.1.0.12:8443/status

# Welche IPs hängen am Interface
ip -4 addr show eth0

# Manuell IP umhängen (vipd sollte gestoppt sein!)
systemctl stop vipd
# ... ggf. manuell mit failover.sh ...

cat failover.sh 
#!/bin/bash
FLOATING_IP_ID="123123123123"
HETZNER_TOKEN="abcabcabc"
FLOATING_IP="195.201.251.2"
SERVER_ID="$(curl -s http://169.254.169.254/hetzner/v1/metadata/instance-id)"

case "$1" in
    master)
        # Hetzner API: Floating IP auf diesen Server umhängen
        curl -s -X POST \
          "https://api.hetzner.cloud/v1/floating_ips/${FLOATING_IP_ID}/actions/assign" \
          -H "Authorization: Bearer ${HETZNER_TOKEN}" \
          -H "Content-Type: application/json" \
          -d "{\"server\": ${SERVER_ID}}"

        # Lokal: IP dem Interface zuweisen
        ip addr add ${FLOATING_IP}/32 dev eth0
        ;;
    backup|fault)
        # Lokal: IP vom Interface entfernen
        ip addr del ${FLOATING_IP}/32 dev eth0 2>/dev/null
	;;
esac

systemctl start vipd
```
