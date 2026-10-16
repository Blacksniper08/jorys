
import ctypes
import ipaddress
import json
import os
import platform
import subprocess
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOST = "127.0.0.1"
PORT = 8765
ROOT = Path(__file__).resolve().parent
MAX_BODY = 8192

ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {
    f"http://127.0.0.1:{PORT}",
    f"http://localhost:{PORT}",
}

SETTINGS = {
    "network": "ms-settings:network-status",
    "display": "ms-settings:display",
    "sound": "ms-settings:sound",
    "apps": "ms-settings:appsfeatures",
    "startup": "ms-settings:startup",
    "update": "ms-settings:windowsupdate",
    "privacy": "ms-settings:privacy",
    "power": "powercfg.cpl",
}


def run_powershell(script, timeout=10):
    result = subprocess.run(
        [
            "powershell.exe", "-NoLogo", "-NoProfile",
            "-NonInteractive", "-Command", script
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout or "PowerShell a échoué").strip()
        )
    return result.stdout.strip()


def get_adapters():
    script = r"""
    $ErrorActionPreference = 'Stop'
    Get-NetAdapter -ErrorAction SilentlyContinue |
      Select-Object @{N='name';E={$_.Name}},
                    @{N='status';E={$_.Status.ToString()}},
                    @{N='speed';E={$_.LinkSpeed.ToString()}} |
      ConvertTo-Json -Compress
    """
    output = run_powershell(script)
    if not output:
        return []
    data = json.loads(output)
    if isinstance(data, dict):
        data = [data]
    return [
        {"name": str(a["name"]), "status": str(a["status"]),
         "speed": str(a.get("speed", ""))}
        for a in data if a.get("name")
    ]


def ps_quote(value):
    """Escape a string as a PowerShell single-quoted literal."""
    return "'" + str(value).replace("'", "''") + "'"


def validate_network(data):
    if not isinstance(data, dict):
        raise ValueError("Corps de requête invalide.")

    name = data.get("adapter")
    mode = data.get("mode")

    if not isinstance(name, str) or not name or len(name) > 200:
        raise ValueError("Choisis une carte réseau valide.")

    # Match against real adapter names, not arbitrary PowerShell input.
    names = {a["name"] for a in get_adapters()}
    if name not in names:
        raise ValueError("Carte réseau inconnue ou déconnectée.")

    if mode not in ("dhcp", "static"):
        raise ValueError("Mode réseau non autorisé.")

    if mode == "dhcp":
        return name, mode, None, None, None, []

    try:
        address = ipaddress.IPv4Address(data.get("ip", ""))
        prefix = int(data.get("prefix", 0))
    except (ValueError, TypeError):
        raise ValueError("Adresse IPv4 ou préfixe invalide.")

    if address.is_loopback or address.is_multicast or address.is_unspecified:
        raise ValueError("Cette adresse IPv4 ne peut pas être utilisée ici.")
    if not 1 <= prefix <= 32:
        raise ValueError("Le préfixe doit être compris entre 1 et 32.")

    gateway_text = data.get("gateway", "").strip()
    gateway = None
    network = ipaddress.IPv4Network(f"{address}/{prefix}", strict=False)

    if gateway_text:
        try:
            gateway = ipaddress.IPv4Address(gateway_text)
        except ValueError:
            raise ValueError("Passerelle IPv4 invalide.")
        if gateway not in network or gateway == address:
            raise ValueError("La passerelle doit être dans le même sous-réseau.")

    dns_values = data.get("dns", [])
    if not isinstance(dns_values, list) or len(dns_values) > 2:
        raise ValueError("Fournis au maximum deux DNS.")
    dns = []
    for item in dns_values:
        try:
            parsed = ipaddress.ip_address(item)
        except ValueError:
            raise ValueError("Adresse DNS invalide.")
        if parsed.is_unspecified or parsed.is_multicast:
            raise ValueError("Adresse DNS non autorisée.")
        dns.append(str(parsed))

    return name, mode, str(address), prefix, str(gateway) if gateway else None, dns


def create_elevated_script(data):
    name, mode, address, prefix, gateway, dns = validate_network(data)
    alias = ps_quote(name)

    # Values inserted below are validated IPs, integers, or escaped literals.
    lines = [
        "$ErrorActionPreference = 'Stop'",
        f"$alias = {alias}",
        "try {",
        "  $adapter = Get-NetAdapter -Name $alias -ErrorAction Stop",
        "  if (-not $adapter) { throw 'Carte réseau introuvable.' }",
        "  Get-NetIPAddress -InterfaceAlias $alias -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue | "
        "Where-Object { $_.PrefixOrigin -eq 'Manual' -and "
        "$_.IPAddress -ne '127.0.0.1' } | "
        "Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue",
    ]

    if mode == "dhcp":
        lines += [
            "  Set-NetIPInterface -InterfaceAlias $alias "
            "-AddressFamily IPv4 -Dhcp Enabled",
            "  Set-DnsClientServerAddress -InterfaceAlias $alias "
            "-ResetServerAddresses",
            "  Write-Host 'DHCP et DNS automatiques demandes.' -ForegroundColor Green",
        ]
    else:
        lines += [
            "  Set-NetIPInterface -InterfaceAlias $alias "
            "-AddressFamily IPv4 -Dhcp Disabled",
        ]
        params = [
            f"-InterfaceAlias $alias",
            f"-IPAddress {ps_quote(address)}",
            f"-PrefixLength {prefix}",
        ]
        if gateway:
            params.append(f"-DefaultGateway {ps_quote(gateway)}")
        lines.append("  New-NetIPAddress " + " ".join(params) + " | Out-Null")

        if dns:
            dns_array = ", ".join(ps_quote(item) for item in dns)
            lines.append(
                "  Set-DnsClientServerAddress -InterfaceAlias $alias "
                f"-ServerAddresses @({dns_array})"
            )
        else:
            lines.append(
                "  Set-DnsClientServerAddress -InterfaceAlias $alias "
                "-ResetServerAddresses"
            )
        lines.append(
            "  Write-Host 'Configuration IP appliquee.' -ForegroundColor Green"
        )

    lines += [
        "} catch {",
        "  Write-Host ('ERREUR : ' + $_.Exception.Message) -ForegroundColor Red",
        "  Write-Host 'Verifie la carte et les parametres reseau.'",
        "}",
        "Write-Host ''",
        "Read-Host 'Appuie sur Entree pour fermer cette fenetre'",
    ]

    fd, path = tempfile.mkstemp(suffix=".ps1", prefix="nexus_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write("\r\n".join(lines))
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def request_uac(script_path):
    # Launch a visible PowerShell window with a standard Windows UAC prompt.
    params = (
        '-NoLogo -NoProfile -ExecutionPolicy RemoteSigned '
        f'-File "{script_path}"'
    )
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "powershell.exe", params, str(ROOT), 1
    )
    if result <= 32:
        try:
            os.unlink(script_path)
        except OSError:
            pass
        raise RuntimeError("Lancement refusé ou impossible. Vérifie l'UAC Windows.")


class Handler(BaseHTTPRequestHandler):
    server_version = "NexusLocal/1.0"

    def log_message(self, fmt, *args):
        # Do not print request bodies or network configuration.
        print("[NEXUS]", fmt % args)

    def send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def guard_host(self):
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            self.send_json(403, {"error": "En-tête Host refusé."})
            return False
        return True

    def guard_origin(self):
        origin = self.headers.get("Origin")
        if origin not in ALLOWED_ORIGINS:
            self.send_json(403, {"error": "Origine web refusée."})
            return False
        return True

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > MAX_BODY:
            raise ValueError("Taille de requête invalide.")
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ValueError("JSON requis.")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        if not self.guard_host():
            return
        path = urlparse(self.path).path

        if path == "/":
            file_path = ROOT / "index.html"
            if not file_path.is_file():
                self.send_error(404)
                return
            body = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/status":
            self.send_json(200, {
                "machine": platform.node(),
                "os": platform.system() + " " + platform.release(),
                "agent": "NEXUS Local",
            })
            return

        if path == "/api/adapters":
            try:
                self.send_json(200, {"adapters": get_adapters()})
            except Exception:
                self.send_json(500, {"error": "Impossible de lire les cartes réseau."})
            return

        self.send_json(404, {"error": "Route inconnue."})

    def do_POST(self):
        if not self.guard_host() or not self.guard_origin():
            return
        path = urlparse(self.path).path

        try:
            data = self.read_json()

            if path == "/api/open-settings":
                name = data.get("name") if isinstance(data, dict) else None
                target = SETTINGS.get(name)
                if not target:
                    self.send_json(400, {"error": "Panneau non autorisé."})
                    return
                os.startfile(target)
                self.send_json(200, {"message": "Panneau Windows demandé."})
                return

            if path == "/api/network":
                script_path = create_elevated_script(data)
                try:
                    request_uac(script_path)
                except Exception:
                    raise
                self.send_json(202, {
                    "message": (
                        "Demande transmise à Windows. Confirme l'UAC "
                        "et vérifie le résultat dans la fenêtre PowerShell."
                    )
                })
                return

            self.send_json(404, {"error": "Route inconnue."})

        except (ValueError, json.JSONDecodeError) as e:
            self.send_json(400, {"error": str(e)})
        except Exception as e:
            print("[NEXUS] Erreur interne :", type(e).__name__)
            self.send_json(500, {
                "error": "Action impossible. Vérifie Windows et les autorisations."
            })


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("Cet agent est prévu pour Windows 10.")

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"NEXUS local : http://{HOST}:{PORT}/")
    print("Écoute limitée à cet ordinateur. Ctrl+C pour arrêter.")
    threading.Timer(1.0, lambda: webbrowser.open(f"http://{HOST}:{PORT}/")).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt de NEXUS.")
    finally:
        server.server_close()
