#!/usr/bin/env python3
"""
FinalPing Ground Station Setup Portal
Broadcasts a FinalPing_Setup WiFi hotspot and serves a setup page at 192.168.4.1
User enters their home WiFi credentials + FinalPing token, Pi saves and reboots.
"""

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

AP_SSID    = "FinalPing_Setup"
AP_IP      = "192.168.4.1"
AP_PORT    = 80
INTERFACE  = os.environ.get("AP_INTERFACE", "wlan0")
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

_hostapd_proc = None
_dnsmasq_proc = None


def log(msg):
    from datetime import datetime
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML_FORM = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FinalPing Setup</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0e1a;color:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
.card{background:#0f1117;border:1px solid rgba(255,255,255,.1);border-radius:20px;
      padding:40px 32px;max-width:420px;width:100%}
.brand-sub{font-size:10px;font-weight:700;letter-spacing:.18em;text-transform:uppercase;color:#4b5563;margin-bottom:4px}
.brand{font-size:24px;font-weight:800;color:#f9fafb;margin-bottom:4px}
.brand-line{width:36px;height:2px;background:linear-gradient(90deg,#0ea5e9,transparent);border-radius:999px;margin-bottom:28px}
h2{font-size:16px;font-weight:600;color:#f9fafb;margin-bottom:6px}
p{font-size:13px;color:#6b7280;line-height:1.6;margin-bottom:22px}
label{display:block;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
      color:#4b5563;margin-bottom:5px}
input{width:100%;background:#0a0e1a;border:1px solid rgba(255,255,255,.1);border-radius:8px;
      padding:10px 14px;color:#f9fafb;font-size:14px;margin-bottom:14px;outline:none;
      -webkit-appearance:none}
input:focus{border-color:rgba(14,165,233,.5)}
.divider{height:1px;background:rgba(255,255,255,.07);margin:18px 0}
button{width:100%;background:#0ea5e9;border:none;border-radius:10px;padding:12px;
       color:#fff;font-size:14px;font-weight:700;cursor:pointer;margin-top:6px}
.note{font-size:11px;color:#374151;text-align:center;margin-top:16px;line-height:1.6}
</style>
</head>
<body>
<div class="card">
  <div class="brand-sub">Aircraft Alerts</div>
  <div class="brand">FinalPing</div>
  <div class="brand-line"></div>
  <h2>Ground Station Setup</h2>
  <p>Connect your ground station to your network and FinalPing account.</p>
  <form method="POST" action="/save">
    <label>WiFi Network Name</label>
    <input type="text" name="ssid" placeholder="Your home/FBO WiFi name" required autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false">
    <label>WiFi Password</label>
    <input type="password" name="wifi_password" placeholder="WiFi password" autocomplete="off">
    <div class="divider"></div>
    <label>FinalPing Account Email</label>
    <input type="email" name="email" placeholder="Email used to log into FinalPing" required autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false">
    <button type="submit">Save &amp; Connect</button>
  </form>
  <div class="note">The ground station will reboot and join your network.<br>This page will go offline.</div>
</div>
</body>
</html>"""

HTML_DONE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FinalPing Setup</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0e1a;color:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
.card{background:#0f1117;border:1px solid rgba(255,255,255,.1);border-radius:20px;
      padding:40px 32px;max-width:420px;width:100%;text-align:center}
.icon{width:56px;height:56px;border-radius:50%;background:rgba(34,197,94,.12);
      border:1px solid rgba(34,197,94,.3);display:flex;align-items:center;
      justify-content:center;margin:0 auto 20px}
h2{font-size:18px;font-weight:700;color:#f9fafb;margin-bottom:10px}
p{font-size:13px;color:#6b7280;line-height:1.6}
</style>
</head>
<body>
<div class="card">
  <div class="icon">
    <svg width="24" height="24" fill="none" viewBox="0 0 24 24">
      <path stroke="#22c55e" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/>
    </svg>
  </div>
  <h2>Setup complete</h2>
  <p>Your ground station is rebooting and will connect to your network in a moment.<br><br>You can close this page.</p>
</div>
</body>
</html>"""


# ── HTTP handler ──────────────────────────────────────────────────────────────

class PortalHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self._serve(200, HTML_FORM)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode(errors="replace")
        params = parse_qs(body)

        ssid          = params.get("ssid",          [""])[0].strip()
        wifi_password = params.get("wifi_password", [""])[0]
        email         = params.get("email",         [""])[0].strip().lower()

        self._serve(200, HTML_DONE)

        t = threading.Thread(target=save_and_reboot, args=(ssid, wifi_password, email), daemon=True)
        t.start()

    def _serve(self, code, html):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(html.encode())


# ── Networking ────────────────────────────────────────────────────────────────

def start_ap():
    global _hostapd_proc, _dnsmasq_proc

    hostapd_conf = f"""interface={INTERFACE}
driver=nl80211
ssid={AP_SSID}
hw_mode=g
channel=6
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
"""
    dnsmasq_conf = f"""interface={INTERFACE}
bind-interfaces
dhcp-range=192.168.4.2,192.168.4.20,255.255.255.0,1h
address=/#/{AP_IP}
no-resolv
server=8.8.8.8
"""
    with open("/tmp/fp-hostapd.conf", "w") as f:
        f.write(hostapd_conf)
    with open("/tmp/fp-dnsmasq.conf", "w") as f:
        f.write(dnsmasq_conf)

    # Disable NetworkManager/wpa_supplicant on this interface so we can take over
    subprocess.run(["nmcli", "radio", "wifi", "off"],         capture_output=True)
    subprocess.run(["rfkill", "unblock", "wifi"],             capture_output=True)
    subprocess.run(["ip", "link", "set", INTERFACE, "up"],    capture_output=True)
    subprocess.run(["ip", "addr", "flush", "dev", INTERFACE], capture_output=True)
    subprocess.run(["ip", "addr", "add", f"{AP_IP}/24", "dev", INTERFACE], capture_output=True)

    _hostapd_proc = subprocess.Popen(["hostapd", "/tmp/fp-hostapd.conf"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    _dnsmasq_proc = subprocess.Popen(["dnsmasq", "--keep-in-foreground",
                                      "--conf-file=/tmp/fp-dnsmasq.conf"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    log(f"[OK] Hotspot broadcasting: {AP_SSID}")
    log(f"[OK] Setup portal at http://{AP_IP}")


def save_and_reboot(ssid, wifi_password, email):
    import urllib.request
    time.sleep(1)
    log("Saving WiFi config...")

    # Stop AP first so WiFi interface is free
    if _hostapd_proc:
        _hostapd_proc.terminate()
    if _dnsmasq_proc:
        _dnsmasq_proc.terminate()

    # Configure WiFi — try NetworkManager (Bookworm), fall back to wpa_supplicant (Bullseye)
    nm = subprocess.run(["which", "nmcli"], capture_output=True)
    if nm.returncode == 0:
        subprocess.run(["nmcli", "radio", "wifi", "on"], capture_output=True)
        time.sleep(3)
        subprocess.run(["nmcli", "dev", "wifi", "connect", ssid, "password", wifi_password,
                        "ifname", INTERFACE], capture_output=True)
    else:
        wpa = (f'ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n'
               f'update_config=1\ncountry=US\n\n'
               f'network={{\n    ssid="{ssid}"\n    psk="{wifi_password}"\n}}\n')
        with open("/etc/wpa_supplicant/wpa_supplicant.conf", "w") as f:
            f.write(wpa)
        subprocess.run(["wpa_cli", "-i", INTERFACE, "reconfigure"], capture_output=True)

    # Wait for internet connection then claim device key from backend
    log("Waiting for internet connection...")
    token = None
    for attempt in range(12):  # up to 60s
        time.sleep(5)
        try:
            data = json.dumps({"email": email}).encode()
            req = urllib.request.Request(
                "https://aircraft-tracker-backend-production.up.railway.app/api/ground/claim",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                token = result.get("gs_device_key")
            if token:
                log("[OK] Device key claimed from backend")
                break
        except Exception as e:
            log(f"Claim attempt {attempt + 1}/12: {e}")

    if not token:
        log("[ERR] Could not claim device key — check email and GS access, then reboot")
        # Save email so we can retry on next boot
        with open(CONFIG_FILE, "w") as f:
            json.dump({"pending_email": email}, f)
        return

    with open(CONFIG_FILE, "w") as f:
        json.dump({"token": token}, f)

    log("[OK] Config saved — rebooting...")
    time.sleep(1)
    subprocess.run(["reboot"])


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log("=" * 50)
    log("FinalPing Ground Station — Setup Mode")
    log("=" * 50)
    start_ap()
    server = HTTPServer(("0.0.0.0", AP_PORT), PortalHandler)
    log(f"[OK] Waiting for device to connect to '{AP_SSID}'")
    server.serve_forever()
