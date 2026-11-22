#!/usr/bin/env python3
# Ad Hoc Share - Copyright (C) 2026 cauliflowermossgrape
# SPDX-License-Identifier: GPL-3.0-or-later
"""
adhoc_share.py - Ad Hoc sign an IPA for a friend's iPhone and host an
over-the-air install link on a public HTTPS URL.

What it does (all from one popup window):
  1. Signs into your Apple Developer account with an App Store Connect API key.
  2. Registers your friend's UDID, the app's bundle ID and (if needed) an
     Apple Distribution certificate.
  3. Creates an Ad Hoc provisioning profile (valid ~1 year).
  4. Re-signs the IPA with rcodesign (works on Windows/Linux, no Mac needed).
  5. Serves the IPA + manifest.plist locally and exposes it through a free
     Cloudflare quick tunnel (https://xxxx.trycloudflare.com), then gives you
     a link to send to your friend.

Requirements:  pip install requests PyJWT cryptography
Tools rcodesign and cloudflared are downloaded into the data folder on first
run if they aren't already on PATH.

Data folder (settings, certificate private key, tools):
  Windows  %APPDATA%\\AdHocShare
  macOS    ~/Library/Application Support/AdHocShare
  Linux    ~/.local/share/adhoc-share   (or $XDG_DATA_HOME/adhoc-share)
Override with the ADHOC_DATA_DIR environment variable.
"""

import base64
import html
import io
import json
import os
import platform
import plistlib
import queue
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import webbrowser
import zipfile
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import jwt
    import requests
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except ImportError:
    print("Missing packages. Run:  pip install requests PyJWT cryptography")
    sys.exit(1)

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    import tkinter.font as tkfont
except ImportError:
    print("tkinter is missing. On Debian/Ubuntu: sudo apt install python3-tk   "
          "Fedora: sudo dnf install python3-tkinter   Arch: sudo pacman -S tk")
    sys.exit(1)

APP_TITLE = "Ad Hoc Share"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))  # bundled assets inside the exe


def _user_data_dir():
    if os.environ.get("ADHOC_DATA_DIR"):
        return Path(os.environ["ADHOC_DATA_DIR"]).expanduser()
    if IS_WIN:
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "AdHocShare"
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "AdHocShare"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "adhoc-share"


DATA = _user_data_dir()  # holds your distribution private key - keep private
TOOLS = DATA / "tools"
CONFIG = DATA / "config.json"
ASC = "https://api.appstoreconnect.apple.com/v1"
API_KEYS_URL = "https://appstoreconnect.apple.com/access/integrations/api"
UDID_HELP_URL = "https://udid.tech"
# Optional: preset your Issuer ID via env var. It's also remembered in config.json.
ISSUER_ID = os.environ.get("ADHOC_ISSUER_ID", "")
RCODESIGN_VERSION = "0.29.0"
UDID_RE = re.compile(r"^([0-9a-fA-F]{40}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16})$")
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WIN else 0


def migrate_legacy_data():
    """Copy ./adhoc_data from older versions so the existing certificate keeps working."""
    legacy = APP_DIR / "adhoc_data"
    if (legacy / "dist_key.pem").exists() and not (DATA / "dist_key.pem").exists():
        DATA.mkdir(parents=True, exist_ok=True)
        for f in legacy.iterdir():
            if f.is_file():
                shutil.copy2(f, DATA / f.name)
        return legacy
    return None


def read_ipa_info(ipa_path):
    """Name, version, bundle ID, minimum iOS and extension count of an IPA."""
    with zipfile.ZipFile(ipa_path) as z:
        names = z.namelist()
        plist = next((n for n in names if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n)), None)
        if not plist:
            raise ValueError("This file has no Payload/*.app inside. Is it an iOS app?")
        info = plistlib.loads(z.read(plist))
        app_dir = plist[: -len("Info.plist")]
        exts = {n[len(app_dir):].split("/")[1] for n in names
                if re.match(re.escape(app_dir) + r"(PlugIns|Extensions)/[^/]+\.appex/", n)}
    return {
        "name": info.get("CFBundleDisplayName") or info.get("CFBundleName") or Path(app_dir).stem,
        "version": info.get("CFBundleShortVersionString", "?"),
        "bundle_id": info.get("CFBundleIdentifier", "?"),
        "min_ios": info.get("MinimumOSVersion"),
        "extensions": len(exts),
    }


# --------------------------------------------------------------------------
# App Store Connect API
# --------------------------------------------------------------------------
class AppStoreConnect:
    def __init__(self, issuer_id, key_id, p8_path):
        self.issuer_id = issuer_id.strip()
        self.key_id = key_id.strip()
        self.private_key = Path(p8_path).read_text()
        self._token, self._token_exp = None, 0

    def _auth(self):
        now = int(time.time())
        if not self._token or now > self._token_exp - 60:
            self._token_exp = now + 15 * 60
            self._token = jwt.encode(
                {"iss": self.issuer_id, "iat": now, "exp": self._token_exp,
                 "aud": "appstoreconnect-v1"},
                self.private_key, algorithm="ES256",
                headers={"kid": self.key_id, "typ": "JWT"})
        return {"Authorization": f"Bearer {self._token}"}

    def call(self, method, path, **kw):
        r = requests.request(method, ASC + path, headers=self._auth(), timeout=60, **kw)
        if r.status_code >= 400:
            try:
                errs = r.json().get("errors", [])
                msg = "; ".join(f"{e.get('title')}: {e.get('detail')}" for e in errs)
            except ValueError:
                msg = r.text[:500]
            raise RuntimeError(f"Apple API {method} {path} -> {r.status_code}: {msg}")
        return r.json() if r.content else {}

    def get(self, path, params=None):
        return self.call("GET", path, params=params)

    def post(self, path, data):
        return self.call("POST", path, json={"data": data})

    def delete(self, path):
        return self.call("DELETE", path)


# --------------------------------------------------------------------------
# Signing identity / device / bundle / profile
# --------------------------------------------------------------------------
def ensure_certificate(api, log):
    """Reuse the stored Apple Distribution cert if still valid, else create one."""
    key_file, cert_file, id_file = DATA / "dist_key.pem", DATA / "dist_cert.pem", DATA / "dist_cert_id.txt"
    if key_file.exists() and cert_file.exists() and id_file.exists():
        cert_id = id_file.read_text().strip()
        try:
            info = api.get(f"/certificates/{cert_id}")["data"]["attributes"]
            exp = datetime.fromisoformat(info["expirationDate"].replace("Z", "+00:00"))
            if exp - datetime.now(timezone.utc) > timedelta(days=330):
                log(f"Reusing distribution certificate (expires {exp:%Y-%m-%d}).")
                return cert_id, key_file, cert_file
            log(f"Stored certificate expires {exp:%Y-%m-%d}; creating a fresh one for a full year.")
        except RuntimeError:
            log("Stored certificate no longer exists on Apple's side; creating a new one.")

    log("Creating a new Apple Distribution certificate...")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AdHoc Share")]))
           .sign(key, hashes.SHA256()))
    try:
        res = api.post("/certificates", {
            "type": "certificates",
            "attributes": {"certificateType": "DISTRIBUTION",
                           "csrContent": csr.public_bytes(serialization.Encoding.PEM).decode()}})
    except RuntimeError as e:
        raise RuntimeError(
            f"{e}\n\nIf you've hit the distribution certificate limit, revoke an unused one at "
            "developer.apple.com > Certificates (apps signed with a revoked cert stop launching).")
    der = base64.b64decode(res["data"]["attributes"]["certificateContent"])
    cert = x509.load_der_x509_certificate(der)
    DATA.mkdir(parents=True, exist_ok=True)
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    id_file.write_text(res["data"]["id"])
    log(f"Certificate created (expires {res['data']['attributes']['expirationDate'][:10]}).")
    return res["data"]["id"], key_file, cert_file


def ensure_device(api, udid, name, log):
    found = api.get("/devices", {"filter[udid]": udid})["data"]
    if found:
        log(f"Device already registered: {found[0]['attributes']['name']}")
        return
    api.post("/devices", {"type": "devices",
                          "attributes": {"name": name, "udid": udid, "platform": "IOS"}})
    log(f"Registered device '{name}'.")


def ensure_bundle_id(api, identifier, log):
    for b in api.get("/bundleIds", {"filter[identifier]": identifier, "limit": 200})["data"]:
        if b["attributes"]["identifier"] == identifier:
            log(f"Bundle ID {identifier} found.")
            return b["id"]
    name = re.sub(r"[^A-Za-z0-9 ]", " ", identifier).strip()[:50] or "AdHoc App"
    try:
        res = api.post("/bundleIds", {"type": "bundleIds", "attributes": {
            "identifier": identifier, "name": name, "platform": "IOS"}})
    except RuntimeError as e:
        raise RuntimeError(f"{e}\n\nThat bundle ID may belong to another team. "
                           "Fill in 'New bundle ID' (e.g. com.yourname.appname) and retry.")
    log(f"Registered bundle ID {identifier}.")
    return res["data"]["id"]


def create_profile(api, bundle_pk, bundle_identifier, cert_id, log):
    prefix = f"AdHocShare {bundle_identifier}"
    for p in api.get("/profiles", {"filter[name]": prefix, "limit": 200})["data"]:
        if p["attributes"]["name"].rsplit(" ", 1)[0] == prefix:
            api.delete(f"/profiles/{p['id']}")
    devices = api.get("/devices", {"filter[platform]": "IOS", "filter[status]": "ENABLED",
                                   "limit": 200})["data"]
    res = api.post("/profiles", {
        "type": "profiles",
        "attributes": {"name": f"{prefix} {datetime.now():%Y%m%d-%H%M%S}",
                       "profileType": "IOS_APP_ADHOC"},
        "relationships": {
            "bundleId": {"data": {"type": "bundleIds", "id": bundle_pk}},
            "certificates": {"data": [{"type": "certificates", "id": cert_id}]},
            "devices": {"data": [{"type": "devices", "id": d["id"]} for d in devices]}}})
    attrs = res["data"]["attributes"]
    log(f"Ad Hoc profile created for {len(devices)} device(s), expires {attrs['expirationDate'][:10]}.")
    return base64.b64decode(attrs["profileContent"])


def profile_plist(profile_bytes):
    start = profile_bytes.index(b"<?xml")
    end = profile_bytes.index(b"</plist>") + len(b"</plist>")
    return plistlib.loads(profile_bytes[start:end])


def entitlements_for(profile_bytes, bundle_identifier):
    """Entitlements matching the profile, with wildcards made concrete (like Xcode)."""
    ents = dict(profile_plist(profile_bytes).get("Entitlements", {}))
    team = ents.get("com.apple.developer.team-identifier") or \
        ents["application-identifier"].split(".", 1)[0]
    ents["application-identifier"] = f"{team}.{bundle_identifier}"
    ents["keychain-access-groups"] = [f"{team}.{bundle_identifier}"]
    ents["get-task-allow"] = False
    return ents


# --------------------------------------------------------------------------
# Tools (rcodesign, cloudflared)
# --------------------------------------------------------------------------
def _download(url, log):
    log(f"Downloading {url.rsplit('/', 1)[-1]} ...")
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def get_rcodesign(log):
    exe = "rcodesign.exe" if IS_WIN else "rcodesign"
    if shutil.which("rcodesign"):
        return shutil.which("rcodesign")
    local = TOOLS / exe
    if local.exists():
        return str(local)
    machine = platform.machine().lower()
    arch = "aarch64" if machine in ("arm64", "aarch64") else "x86_64"
    if IS_WIN:
        asset = f"apple-codesign-{RCODESIGN_VERSION}-{arch}-pc-windows-msvc.zip"
    elif sys.platform == "darwin":
        asset = f"apple-codesign-{RCODESIGN_VERSION}-macos-universal.tar.gz"
    else:
        asset = f"apple-codesign-{RCODESIGN_VERSION}-{arch}-unknown-linux-musl.tar.gz"
    url = ("https://github.com/indygreg/apple-platform-rs/releases/download/"
           f"apple-codesign%2F{RCODESIGN_VERSION}/{asset}")
    blob = _download(url, log)
    TOOLS.mkdir(parents=True, exist_ok=True)
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            member = next(n for n in z.namelist() if n.endswith("/" + exe))
            local.write_bytes(z.read(member))
    else:
        with tarfile.open(fileobj=io.BytesIO(blob)) as t:
            member = next(m for m in t.getmembers() if m.name.endswith("/" + exe))
            local.write_bytes(t.extractfile(member).read())
        local.chmod(0o755)
    return str(local)


def get_cloudflared(log):
    if shutil.which("cloudflared"):
        return shutil.which("cloudflared")
    exe = "cloudflared.exe" if IS_WIN else "cloudflared"
    local = TOOLS / exe
    if local.exists():
        return str(local)
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "amd64"
    if IS_WIN:
        asset = f"cloudflared-windows-{arch}.exe"
    elif sys.platform == "darwin":
        raise RuntimeError("Install cloudflared first:  brew install cloudflared")
    else:
        asset = f"cloudflared-linux-{arch}"
    blob = _download(f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}", log)
    TOOLS.mkdir(parents=True, exist_ok=True)
    local.write_bytes(blob)
    local.chmod(0o755)
    return str(local)


# --------------------------------------------------------------------------
# Re-signing
# --------------------------------------------------------------------------
def resign_ipa(ipa_path, new_bundle_id, api, udid, device_name, log):
    work = Path(tempfile.mkdtemp(prefix="adhoc_"))
    log("Unpacking IPA...")
    with zipfile.ZipFile(ipa_path) as z:
        z.extractall(work)
    apps = list((work / "Payload").glob("*.app"))
    if not apps:
        raise RuntimeError("No Payload/*.app found inside the IPA.")
    app = apps[0]
    info_path = app / "Info.plist"
    info = plistlib.loads(info_path.read_bytes())

    orig_bundle_id = info["CFBundleIdentifier"]
    if new_bundle_id:
        log(f"Changing bundle ID {orig_bundle_id} -> {new_bundle_id}")
        info["CFBundleIdentifier"] = new_bundle_id
        info_path.write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_BINARY))
    bundle_identifier = info["CFBundleIdentifier"]

    # Watch apps need watchOS profiles; drop them (Sideloadly does the same).
    if (app / "Watch").exists():
        log("Removing Watch/ app (not supported for ad hoc iPhone install).")
        shutil.rmtree(app / "Watch")

    # Every extension gets a bundle ID under the main app's ID and its own profile.
    bundles = [(".", app, bundle_identifier)]
    for appex in sorted(list(app.glob("PlugIns/*.appex")) + list(app.glob("Extensions/*.appex"))):
        ex_info_path = appex / "Info.plist"
        ex_info = plistlib.loads(ex_info_path.read_bytes())
        old = ex_info["CFBundleIdentifier"]
        if old.startswith(bundle_identifier + "."):
            new = old
        elif old.startswith(orig_bundle_id + "."):
            new = bundle_identifier + old[len(orig_bundle_id):]
        else:
            new = f"{bundle_identifier}.{re.sub(r'[^A-Za-z0-9-]', '', appex.stem)}"
        if new != old:
            log(f"Extension {appex.name}: bundle ID {old} -> {new}")
            ex_info["CFBundleIdentifier"] = new
            ex_info_path.write_bytes(plistlib.dumps(ex_info, fmt=plistlib.FMT_BINARY))
        bundles.append((appex.relative_to(app).as_posix(), appex, new))

    cert_id, key_file, cert_file = ensure_certificate(api, log)
    ensure_device(api, udid, device_name, log)

    # rcodesign takes "scope:path" and splits on the first ':', so a Windows
    # "C:\..." path breaks it. Use paths relative to the work dir (cwd) instead.
    ent_args = []
    for i, (scope, bundle_dir, bid) in enumerate(bundles):
        bundle_pk = ensure_bundle_id(api, bid, log)
        profile = create_profile(api, bundle_pk, bid, cert_id, log)
        (bundle_dir / "embedded.mobileprovision").write_bytes(profile)
        shutil.rmtree(bundle_dir / "_CodeSignature", ignore_errors=True)
        ent_name = f"ents_{i}.plist"
        (work / ent_name).write_bytes(plistlib.dumps(entitlements_for(profile, bid)))
        if scope == ".":
            ent_args += ["-e", f"@main:{ent_name}"]
        else:  # rcodesign matches nested bundles by OS-native relative path
            for s in {scope, scope.replace("/", os.sep)}:
                ent_args += ["-e", f"{s}:{ent_name}"]

    rcodesign = get_rcodesign(log)
    log(f"Signing app + {len(bundles) - 1} extension(s) with rcodesign...")
    proc = subprocess.run(
        [rcodesign, "sign", "--pem-file", str(key_file), "--pem-file", str(cert_file),
         *ent_args, app.relative_to(work).as_posix()],
        cwd=work, capture_output=True, text=True, stdin=subprocess.DEVNULL,
        creationflags=NO_WINDOW)
    if proc.returncode != 0:
        raise RuntimeError("rcodesign failed:\n" + (proc.stderr or proc.stdout)[-3000:])

    out_dir = DATA / "serve"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    out_ipa = out_dir / "app.ipa"
    log("Repacking IPA...")
    with zipfile.ZipFile(out_ipa, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted((work / "Payload").rglob("*")):
            z.write(f, f.relative_to(work))
    shutil.rmtree(work, ignore_errors=True)
    saved = Path(ipa_path).with_name(f"{Path(ipa_path).stem}-adhoc.ipa")
    try:
        shutil.copy(out_ipa, saved)
    except OSError:  # input folder not writable
        saved = DATA / saved.name
        shutil.copy(out_ipa, saved)
    log(f"Signed IPA saved to {saved}")

    title = info.get("CFBundleDisplayName") or info.get("CFBundleName") or app.stem
    return out_dir, bundle_identifier, info.get("CFBundleShortVersionString", "1.0"), title, saved


# --------------------------------------------------------------------------
# Hosting
# --------------------------------------------------------------------------
def write_install_files(out_dir, base_url, bundle_id, version, title):
    manifest = {"items": [{
        "assets": [{"kind": "software-package", "url": f"{base_url}/app.ipa"}],
        "metadata": {"bundle-identifier": bundle_id, "bundle-version": version,
                     "kind": "software", "title": title}}]}
    (out_dir / "manifest.plist").write_bytes(plistlib.dumps(manifest))
    link = f"itms-services://?action=download-manifest&url={base_url}/manifest.plist"
    page = INSTALL_PAGE.replace("{{title}}", html.escape(title)) \
        .replace("{{version}}", html.escape(version)).replace("{{link}}", html.escape(link))
    (out_dir / "index.html").write_text(page, encoding="utf-8")


INSTALL_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Install {{title}}</title>
<style>
:root{--bg:#f5f5f7;--card:#fff;--text:#1d1d1f;--muted:#5f6368;--accent:#0a66d9;--line:#e3e3e8}
@media (prefers-color-scheme:dark){:root{--bg:#000;--card:#1c1c1e;--text:#f2f2f7;--muted:#a1a1a6;--accent:#3d8bff;--line:#2c2c2e}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px 16px;background:var(--bg);
color:var(--text);font:17px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{width:100%;max-width:400px;background:var(--card);border-radius:20px;padding:32px 24px;text-align:center;
box-shadow:0 1px 2px rgba(0,0,0,.06),0 8px 24px rgba(0,0,0,.06)}
h1{margin:0 0 4px;font-size:28px;line-height:1.2;letter-spacing:-.01em;text-wrap:balance}
.ver{margin:0;color:var(--muted);font-size:14px}
a.btn{display:block;margin:28px 0 24px;padding:15px 20px;border-radius:14px;background:var(--accent);color:#fff;
font-weight:600;text-decoration:none;font-size:18px}
a.btn:active{opacity:.85}
ol{margin:0;padding:20px 0 0;border-top:1px solid var(--line);text-align:left;color:var(--muted);font-size:14px;
list-style-position:inside}
li+li{margin-top:6px}
</style></head><body><main>
<h1>{{title}}</h1><p class="ver">Version {{version}}</p>
<a class="btn" href="{{link}}">Install</a>
<ol><li>Open this page in Safari.</li><li>Tap Install, then Install again.</li>
<li>Go to your home screen to watch it download.</li></ol>
</main></body></html>
"""


class QuietHandler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map,
                      ".ipa": "application/octet-stream", ".plist": "text/xml"}

    def log_message(self, *a):
        pass


def start_hosting(out_dir, log):
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(out_dir)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    cloudflared = get_cloudflared(log)
    log("Starting Cloudflare tunnel (free, no account)...")
    proc = subprocess.Popen(
        [cloudflared, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        text=True, creationflags=NO_WINDOW)
    url, deadline = None, time.time() + 60
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line and proc.poll() is not None:
            break
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m:
            url = m.group(0)
            break
    if not url:
        proc.kill()
        server.shutdown()
        raise RuntimeError("Could not get a tunnel URL from cloudflared.")
    threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()  # drain
    return server, proc, url


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
LIGHT = {
    "bg": "#FFFFFF", "sunken": "#F4F5F7", "text": "#1D1D1F", "muted": "#5F6368",
    "line": "#DCDEE3", "field": "#FFFFFF", "field_line": "#C3C7CE",
    "accent": "#0A66D9", "accent_hover": "#0857B9", "accent_press": "#074A9E",
    "on_accent": "#FFFFFF", "link": "#0A5BC4", "success": "#1A7F37", "success_bg": "#EDF7EF",
    "success_line": "#BFE3C7", "error": "#C5221F", "disabled": "#8E949B",
    "btn": "#F1F2F4", "btn_hover": "#E5E7EA", "btn_press": "#D9DCE0",
}
DARK = {
    "bg": "#1E1F22", "sunken": "#16171A", "text": "#EDEEF0", "muted": "#A3A7AD",
    "line": "#34363B", "field": "#26282C", "field_line": "#46494F",
    "accent": "#2A6AD1", "accent_hover": "#3478E0", "accent_press": "#2259B3",
    "on_accent": "#FFFFFF", "link": "#6AA6FF", "success": "#5BCB79", "success_bg": "#1B2A1F",
    "success_line": "#2D4A35", "error": "#FF7B72", "disabled": "#6E737A",
    "btn": "#2C2E33", "btn_hover": "#363940", "btn_press": "#40444B",
}
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


def system_prefers_dark():
    forced = os.environ.get("ADHOC_THEME", "").lower()
    if forced in ("dark", "light"):
        return forced == "dark"
    try:
        if IS_WIN:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
        if IS_MAC:
            out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                 capture_output=True, text=True, timeout=2)
            return "dark" in out.stdout.lower()
        if "dark" in os.environ.get("GTK_THEME", "").lower():
            return True
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                             capture_output=True, text=True, timeout=2, stdin=subprocess.DEVNULL)
        return "dark" in out.stdout.lower()
    except Exception:  # noqa: BLE001 - any failure means "use light"
        return False


def _first_font(root, candidates, fallback):
    families = set(tkfont.families(root))
    return next((f for f in candidates if f in families), fallback)


class App:
    LABEL_WIDTH = 120

    def __init__(self, root):
        self.root, self.q = root, queue.Queue()
        self.server = self.tunnel = None
        self.running = False
        self.dark = system_prefers_dark()
        self.c = DARK if self.dark else LIGHT
        self._setup_style()

        root.title(APP_TITLE)
        root.configure(bg=self.c["bg"])
        self._set_icon()

        cfg = {}
        try:
            cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
        except (OSError, ValueError):
            pass

        self.vars = {k: tk.StringVar() for k in
                     ("issuer", "key_id", "p8", "ipa", "bundle", "udid", "device")}
        self.vars["issuer"].set(cfg.get("issuer") or ISSUER_ID)
        self.vars["key_id"].set(cfg.get("key_id", ""))
        self.vars["p8"].set(cfg.get("p8", ""))
        self.vars["device"].set(cfg.get("device") or "Friend's iPhone")
        self.inputs = []  # widgets disabled while working
        self.hints = []   # helper text under fields; wrapped to the field column

        outer = ttk.Frame(root, padding=(28, 24, 28, 20))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        self.outer = outer

        ttk.Label(outer, text=APP_TITLE, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(outer, text="Sign an iOS app for a friend's iPhone and send them an install link.",
                  style="Muted.TLabel").grid(row=0 + 1, column=0, sticky="w", pady=(2, 0))

        # Apple account
        sec = self._section(outer, 2, "Apple account", ("Get an API key", API_KEYS_URL))
        self._entry_row(sec, 1, "Issuer ID", "issuer")
        self._entry_row(sec, 2, "API key file", "p8", browse="p8")
        self._entry_row(sec, 3, "Key ID", "key_id", width=16, stretch=False)
        self._hint(sec, 4, "Filled in from the AuthKey_XXXXXXXXXX.p8 file name.")
        self.vars["p8"].trace_add("write", lambda *_: self._autofill_key_id())

        # App
        sec = self._section(outer, 3, "App")
        self._entry_row(sec, 1, "IPA file", "ipa", browse="ipa")
        self.ipa_info = self._hint(sec, 2, "Choose the .ipa you want to send.")
        self._entry_row(sec, 3, "New bundle ID", "bundle")
        self._hint(sec, 4, "Optional. Only needed if Apple says the bundle ID is already taken.")

        # Friend's iPhone
        sec = self._section(outer, 4, "Friend's iPhone", ("How to find a UDID", UDID_HELP_URL))
        self.udid_entry = self._entry_row(sec, 1, "UDID", "udid")
        self.udid_hint = self._hint(sec, 2, "Looks like 00008110-001A2B3C4D5E6F70.")
        self._entry_row(sec, 3, "Device name", "device")
        self._hint(sec, 4, "Only a label in your Apple account.")
        self.vars["udid"].trace_add("write", lambda *_: self._check_udid())

        # Action bar
        ttk.Separator(outer).grid(row=5, column=0, sticky="ew", pady=(22, 16))
        bar = ttk.Frame(outer)
        bar.grid(row=6, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)
        self.status = ttk.Label(bar, text="Fill in the fields above, then sign.",
                                style="Muted.TLabel", justify="left")
        self.status.grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(bar, mode="indeterminate",
                                        style="Accent.Horizontal.TProgressbar")
        self.go = ttk.Button(bar, text="Sign and share", style="Accent.TButton", command=self.start)
        self.go.grid(row=0, column=1, rowspan=2, sticky="e", padx=(16, 0))

        # Result panel (hidden until a link exists)
        self.result = tk.Frame(outer, bg=self.c["success_bg"], highlightthickness=1,
                               highlightbackground=self.c["success_line"], padx=16, pady=14)
        self.result.columnconfigure(0, weight=1)
        tk.Label(self.result, text="Ready to install", bg=self.c["success_bg"], fg=self.c["success"],
                 font=self.f_section).grid(row=0, column=0, sticky="w")
        ttk.Button(self.result, text="Stop sharing", command=self.stop_sharing
                   ).grid(row=0, column=1, columnspan=2, sticky="e")
        note = tk.Label(self.result, bg=self.c["success_bg"], fg=self.c["text"], font=self.f_small,
                        text="Send this link to your friend to open in Safari. Keep this window "
                             "open until they've installed the app.", justify="left", anchor="w")
        note.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 10))
        self.link = tk.StringVar()
        ttk.Entry(self.result, textvariable=self.link, state="readonly",
                  style="Link.TEntry").grid(row=2, column=0, sticky="ew")
        self.copy_btn = ttk.Button(self.result, text="Copy link", command=self.copy, width=10)
        self.copy_btn.grid(row=2, column=1, padx=(8, 0))
        ttk.Button(self.result, text="Open", command=self.open_link).grid(row=2, column=2, padx=(8, 0))
        self.saved_label = tk.Label(self.result, bg=self.c["success_bg"], fg=self.c["muted"],
                                    font=self.f_small, justify="left", anchor="w")
        self.saved_label.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.panel_text = [note, self.saved_label]

        # Details (log)
        self.details_toggle = ttk.Label(outer, text="Show details", style="Link.TLabel",
                                        cursor="hand2")
        self.details_toggle.grid(row=8, column=0, sticky="w", pady=(14, 0))
        self._linkify(self.details_toggle, self.toggle_details)
        self.log_frame = ttk.Frame(outer)
        self.log_frame.columnconfigure(0, weight=1)
        self.log_frame.rowconfigure(0, weight=1)
        self.logbox = tk.Text(self.log_frame, height=10, wrap="word", font=self.f_mono,
                              bg=self.c["sunken"], fg=self.c["text"], relief="flat",
                              highlightthickness=1, highlightbackground=self.c["line"],
                              highlightcolor=self.c["line"], padx=10, pady=8,
                              insertbackground=self.c["text"], state="disabled", cursor="arrow")
        self.logbox.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(self.log_frame, command=self.logbox.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.logbox.configure(yscrollcommand=sb.set)

        outer.bind("<Configure>", self._rewrap)
        root.bind("<Return>", lambda e: self.start() if not self.running else None)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.pump)

        moved = migrate_legacy_data()
        if moved:
            self.log(f"Copied your saved certificate and settings from {moved} to {DATA}.")
        root.update_idletasks()
        root.minsize(640, root.winfo_reqheight())
        root.geometry(f"700x{root.winfo_reqheight()}")
        self._dark_title_bar()

    # ----- styling --------------------------------------------------------
    def _setup_style(self):
        c, root = self.c, self.root
        if IS_WIN:
            ui = _first_font(root, ["Segoe UI"], "TkDefaultFont")
            ui_bold = _first_font(root, ["Segoe UI Semibold"], ui)
            mono = _first_font(root, ["Cascadia Mono", "Consolas"], "TkFixedFont")
        elif IS_MAC:
            ui = ui_bold = _first_font(root, [".AppleSystemUIFont", "Helvetica Neue"], "TkDefaultFont")
            mono = _first_font(root, ["Menlo", "Monaco"], "TkFixedFont")
        else:
            ui = ui_bold = _first_font(root, ["Inter", "Cantarell", "Ubuntu", "Noto Sans",
                                              "DejaVu Sans", "Liberation Sans"], "TkDefaultFont")
            mono = _first_font(root, ["JetBrains Mono", "Ubuntu Mono", "DejaVu Sans Mono",
                                      "Noto Sans Mono", "Liberation Mono"], "TkFixedFont")
        semibold = "normal" if ui_bold != ui else "bold"
        self.f_body = (ui, 10)
        self.f_small = (ui, 9)
        self.f_title = (ui_bold, 16, semibold)
        self.f_section = (ui_bold, 11, semibold)
        self.f_button = (ui_bold, 10, semibold)
        self.f_mono = (mono, 9)
        self.f_link = tkfont.Font(root, family=ui, size=9)
        self.f_link_hover = tkfont.Font(root, family=ui, size=9, underline=True)

        st = ttk.Style(root)
        st.theme_use("clam")
        st.configure(".", background=c["bg"], foreground=c["text"], font=self.f_body,
                     bordercolor=c["line"], troughcolor=c["sunken"], focuscolor=c["accent"],
                     selectbackground=c["accent"], selectforeground=c["on_accent"],
                     insertcolor=c["text"])
        st.configure("TFrame", background=c["bg"])
        st.configure("TLabel", background=c["bg"], foreground=c["text"], font=self.f_body)
        st.configure("Title.TLabel", font=self.f_title)
        st.configure("Section.TLabel", font=self.f_section)
        st.configure("Field.TLabel", foreground=c["text"], font=self.f_body)
        st.configure("Muted.TLabel", foreground=c["muted"], font=self.f_small)
        st.configure("Ok.TLabel", foreground=c["success"], font=self.f_small)
        st.configure("Error.TLabel", foreground=c["error"], font=self.f_small)
        st.configure("Link.TLabel", foreground=c["link"], font=self.f_link)
        st.configure("LinkHover.TLabel", foreground=c["link"], font=self.f_link_hover)

        field = dict(fieldbackground=c["field"], foreground=c["text"], bordercolor=c["field_line"],
                     lightcolor=c["field"], darkcolor=c["field"], insertcolor=c["text"],
                     padding=(8, 6))
        st.configure("TEntry", **field)
        st.map("TEntry",
               bordercolor=[("disabled", c["line"]), ("focus", c["accent"])],
               lightcolor=[("focus", c["accent"])],
               fieldbackground=[("disabled", c["sunken"]), ("readonly", c["sunken"])],
               foreground=[("disabled", c["disabled"])])
        st.configure("Invalid.TEntry", **{**field, "bordercolor": c["error"],
                                          "lightcolor": c["error"]})
        st.map("Invalid.TEntry", bordercolor=[("focus", c["error"])],
               lightcolor=[("focus", c["error"])])
        st.configure("Link.TEntry", **{**field, "fieldbackground": c["field"],
                                       "bordercolor": c["success_line"]})
        st.map("Link.TEntry", fieldbackground=[("readonly", c["field"])])

        st.configure("TButton", background=c["btn"], foreground=c["text"], bordercolor=c["field_line"],
                     lightcolor=c["btn"], darkcolor=c["btn"], padding=(12, 5), relief="flat",
                     focusthickness=1, focuscolor=c["accent"], anchor="center")
        st.map("TButton",
               background=[("disabled", c["sunken"]), ("pressed", c["btn_press"]),
                           ("active", c["btn_hover"])],
               lightcolor=[("disabled", c["sunken"]), ("pressed", c["btn_press"]),
                           ("active", c["btn_hover"])],
               darkcolor=[("disabled", c["sunken"]), ("pressed", c["btn_press"]),
                          ("active", c["btn_hover"])],
               bordercolor=[("disabled", c["line"]), ("focus", c["accent"])],
               foreground=[("disabled", c["disabled"])])
        st.configure("Accent.TButton", background=c["accent"], foreground=c["on_accent"],
                     bordercolor=c["accent"], lightcolor=c["accent"], darkcolor=c["accent"],
                     font=self.f_button, padding=(20, 8), focuscolor=c["on_accent"])
        st.map("Accent.TButton",
               background=[("disabled", c["btn"]), ("pressed", c["accent_press"]),
                           ("active", c["accent_hover"])],
               lightcolor=[("disabled", c["btn"]), ("pressed", c["accent_press"]),
                           ("active", c["accent_hover"])],
               darkcolor=[("disabled", c["btn"]), ("pressed", c["accent_press"]),
                          ("active", c["accent_hover"])],
               bordercolor=[("disabled", c["line"]), ("pressed", c["accent_press"]),
                            ("active", c["accent_hover"])],
               foreground=[("disabled", c["disabled"])])
        st.configure("Accent.Horizontal.TProgressbar", background=c["accent"],
                     troughcolor=c["sunken"], bordercolor=c["sunken"], lightcolor=c["accent"],
                     darkcolor=c["accent"], thickness=4)
        st.configure("TSeparator", background=c["line"])
        st.configure("Vertical.TScrollbar", background=c["btn"], troughcolor=c["sunken"],
                     bordercolor=c["sunken"], arrowcolor=c["muted"], lightcolor=c["btn"],
                     darkcolor=c["btn"], gripcount=0)
        st.map("Vertical.TScrollbar", background=[("active", c["btn_hover"])])
        root.option_add("*TCombobox*Listbox.font", self.f_body)

    def _set_icon(self):
        icon = RES_DIR / "assets" / "icon.png"
        if icon.exists():
            try:
                self._icon = tk.PhotoImage(file=str(icon))
                self.root.iconphoto(True, self._icon)
            except tk.TclError:
                pass

    def _dark_title_bar(self):
        if not (IS_WIN and self.dark):
            return
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            value = ctypes.c_int(1)
            for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new, old builds)
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
        except Exception:  # noqa: BLE001 - cosmetic only
            pass

    # ----- layout helpers -------------------------------------------------
    def _section(self, parent, row, title, help_link=None):
        sec = ttk.Frame(parent)
        sec.grid(row=row, column=0, sticky="ew", pady=(24, 0))
        sec.columnconfigure(1, weight=1, minsize=0)
        scale = float(self.root.tk.call("tk", "scaling")) / (96 / 72)
        sec.columnconfigure(0, minsize=int(self.LABEL_WIDTH * max(scale, 1)))
        head = ttk.Frame(sec)
        head.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text=title, style="Section.TLabel").grid(row=0, column=0, sticky="w")
        if help_link:
            text, url = help_link
            lbl = ttk.Label(head, text=text, style="Link.TLabel", cursor="hand2")
            lbl.grid(row=0, column=1, sticky="e")
            self._linkify(lbl, lambda u=url: webbrowser.open(u))
        return sec

    def _entry_row(self, sec, row, label, key, browse=None, width=None, stretch=True):
        ttk.Label(sec, text=label, style="Field.TLabel").grid(row=row, column=0, sticky="w",
                                                              padx=(0, 16), pady=(6, 0))
        e = ttk.Entry(sec, textvariable=self.vars[key], width=width or 20)
        e.grid(row=row, column=1, columnspan=1 if browse else 2,
               sticky="ew" if stretch else "w", pady=(6, 0))
        self.inputs.append(e)
        if browse:
            b = ttk.Button(sec, text="Choose…", command=partial(self.browse, key, browse))
            b.grid(row=row, column=2, sticky="e", padx=(8, 0), pady=(6, 0))
            self.inputs.append(b)
        return e

    def _hint(self, sec, row, text):
        lbl = ttk.Label(sec, text=text, style="Muted.TLabel", justify="left")
        self.hints.append(lbl)
        lbl.grid(row=row, column=1, columnspan=2, sticky="w", pady=(3, 2))
        return lbl

    def _rewrap(self, event=None):
        """Wrap helper text to the space it actually has (DPI and font independent)."""
        inner = self.outer.winfo_width() - 56  # outer padding
        label_col = self.hints[0].master.grid_bbox(0, 1)[2] if self.hints else 0
        for h in self.hints:
            h.configure(wraplength=max(inner - label_col - 4, 200))
        self.status.configure(wraplength=max(inner - self.go.winfo_width() - 24, 200))
        for t in self.panel_text:
            t.configure(wraplength=max(inner - 36, 200))

    def _linkify(self, label, command):
        label.bind("<Button-1>", lambda e: command())
        label.bind("<Enter>", lambda e: label.configure(style="LinkHover.TLabel"))
        label.bind("<Leave>", lambda e: label.configure(style="Link.TLabel"))

    # ----- field behavior -------------------------------------------------
    def browse(self, key, kind):
        if kind == "p8":
            types = [("App Store Connect API key", ("*.p8", "*.P8")), ("All files", "*")]
        else:
            types = [("iOS app", ("*.ipa", "*.IPA")), ("All files", "*")]
        current = self.vars[key].get()
        initial = str(Path(current).parent) if current and Path(current).parent.exists() else None
        p = filedialog.askopenfilename(parent=self.root, filetypes=types, initialdir=initial)
        if p:
            self.vars[key].set(p)
            if key == "ipa":
                self._show_ipa_info(p)

    def _autofill_key_id(self):
        name = re.split(r"[\\/]", self.vars["p8"].get().strip())[-1]  # either separator
        m = re.match(r"AuthKey_([A-Z0-9]{10})\.p8$", name, re.I)
        if m:
            self.vars["key_id"].set(m.group(1).upper())

    def _show_ipa_info(self, path):
        try:
            i = read_ipa_info(path)
        except zipfile.BadZipFile:
            self.ipa_info.configure(text="This isn't a valid .ipa (not a zip archive).",
                                    style="Error.TLabel")
            return
        except OSError as e:
            self.ipa_info.configure(text=f"Can't open this file: {e.strerror or e}.",
                                    style="Error.TLabel")
            return
        except ValueError as e:
            self.ipa_info.configure(text=str(e), style="Error.TLabel")
            return
        parts = [f"{i['name']} {i['version']}", i["bundle_id"]]
        if i["min_ios"]:
            parts.append(f"iOS {i['min_ios']} or later")
        if i["extensions"]:
            parts.append(f"{i['extensions']} extension{'s' if i['extensions'] != 1 else ''}")
        keep = [x.replace(" ", "\u00a0") for x in parts]  # wrap between parts, not inside
        self.ipa_info.configure(text="  ·  ".join(keep), style="Muted.TLabel")

    def _check_udid(self):
        raw = self.vars["udid"].get()
        clean = re.sub(r"\s", "", raw)
        if clean != raw:
            self.vars["udid"].set(clean)
            return
        if not clean:
            self.udid_entry.configure(style="TEntry")
            self.udid_hint.configure(text="Looks like 00008110-001A2B3C4D5E6F70.",
                                     style="Muted.TLabel")
        elif UDID_RE.match(clean):
            self.udid_entry.configure(style="TEntry")
            self.udid_hint.configure(text="Valid UDID.", style="Ok.TLabel")
        else:
            self.udid_entry.configure(style="Invalid.TEntry")
            self.udid_hint.configure(
                text="Not a UDID yet. It's 25 characters with one dash "
                     "(00008110-001A2B3C4D5E6F70), or 40 letters and digits.",
                style="Error.TLabel")

    # ----- state ----------------------------------------------------------
    def log(self, msg):
        self.q.put(("log", msg))

    def set_status(self, text, kind="muted"):
        style = {"muted": "Muted.TLabel", "error": "Error.TLabel", "ok": "Ok.TLabel"}[kind]
        self.status.configure(text=text, style=style)

    def set_running(self, running):
        self.running = running
        for w in self.inputs:
            w.state(["disabled"] if running else ["!disabled"])
        self.go.state(["disabled"] if running else ["!disabled"])
        self.go.configure(text="Working…" if running else "Sign and share")
        if running:
            self.progress.grid(row=1, column=0, sticky="ew", pady=(8, 0))
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.grid_remove()

    def toggle_details(self, show=None):
        visible = self.log_frame.winfo_ismapped()
        show = (not visible) if show is None else show
        if show and not visible:
            self.log_frame.grid(row=9, column=0, sticky="nsew", pady=(8, 0))
            self.outer.rowconfigure(9, weight=1)
            self.details_toggle.configure(text="Hide details")
        elif not show and visible:
            self.log_frame.grid_remove()
            self.outer.rowconfigure(9, weight=0)
            self.details_toggle.configure(text="Show details")
        self._fit_height()

    def _fit_height(self):
        self.root.update_idletasks()
        need = self.root.winfo_reqheight()
        self.root.minsize(640, need if not self.log_frame.winfo_ismapped() else need - 120)
        if self.root.winfo_height() < need or not self.log_frame.winfo_ismapped():
            self.root.geometry(f"{self.root.winfo_width()}x{need}")

    def pump(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", val + "\n")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                    if self.running:
                        self.set_status(val.strip().splitlines()[0].rstrip(".") + "…")
                elif kind == "done":
                    url, saved, title = val
                    self.set_running(False)
                    self.link.set(url)
                    self.saved_label.configure(text=f"Signed IPA saved to {saved}")
                    self.result.grid(row=7, column=0, sticky="ew", pady=(16, 0))
                    self.set_status(f"Sharing {title}.", "ok")
                    self._fit_height()
                elif kind == "error":
                    self.set_running(False)
                    first = val.strip().splitlines()[0]
                    self.set_status(first, "error")
                    self.toggle_details(True)
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    # ----- actions --------------------------------------------------------
    def copy(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.link.get())
        self.copy_btn.configure(text="Copied")
        self.root.after(1500, lambda: self.copy_btn.configure(text="Copy link"))

    def open_link(self):
        if self.link.get():
            webbrowser.open(self.link.get())

    def start(self):
        if self.running:
            return
        v = {k: s.get().strip() for k, s in self.vars.items()}
        checks = [
            ("issuer", lambda x: UUID_RE.match(x),
             "Enter your Issuer ID. It's shown above the keys table on the API keys page."),
            ("p8", lambda x: Path(x).is_file(), "Choose your AuthKey_XXXXXXXXXX.p8 file."),
            ("key_id", lambda x: re.fullmatch(r"[A-Z0-9]{10}", x, re.I),
             "Enter the 10-character Key ID from the API keys page."),
            ("ipa", lambda x: Path(x).is_file(), "Choose the .ipa file to send."),
            ("udid", lambda x: UDID_RE.match(x), "Enter your friend's UDID."),
        ]
        for key, ok, msg in checks:
            if not v[key] or not ok(v[key]):
                self.set_status(msg, "error")
                for w in self.inputs:
                    if isinstance(w, ttk.Entry) and str(w.cget("textvariable")) == str(self.vars[key]):
                        w.focus_set()
                return
        if v["bundle"] and not re.fullmatch(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+", v["bundle"]):
            self.set_status("New bundle ID should look like com.yourname.appname.", "error")
            return
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps({k: v[k] for k in ("issuer", "key_id", "p8", "device")}))
        except OSError as e:
            self.log(f"Couldn't save settings: {e}")
        self.stop_sharing(quiet=True)
        self.set_running(True)
        threading.Thread(target=self.work, args=(v,), daemon=True).start()

    def work(self, v):
        try:
            api = AppStoreConnect(v["issuer"], v["key_id"], v["p8"])
            self.log("Signing in to App Store Connect...")
            try:
                api.get("/devices", {"limit": 1})
            except RuntimeError as e:
                if "401" in str(e):
                    raise RuntimeError("Apple rejected the API key. Check the Issuer ID, Key ID "
                                       "and .p8 file, and that the key hasn't been revoked.") from e
                raise
            self.log("Signed in.")
            out_dir, bid, ver, title, saved = resign_ipa(
                v["ipa"], v["bundle"], api, v["udid"], v["device"] or "Friend's iPhone", self.log)
            self.server, self.tunnel, url = start_hosting(out_dir, self.log)
            write_install_files(out_dir, url, bid, ver, title)
            self.log(f"Install link: {url}")
            self.q.put(("done", (url, saved, title)))
        except requests.ConnectionError:
            self.log("ERROR: no connection to Apple. Check your internet connection.")
            self.q.put(("error", "Can't reach Apple. Check your internet connection and try again."))
        except Exception as e:  # noqa: BLE001 - surface everything to the user
            self.log(f"ERROR: {e}")
            self.q.put(("error", str(e)))

    def stop_sharing(self, quiet=False):
        if self.tunnel:
            self.tunnel.kill()
        if self.server:
            self.server.shutdown()
        was_sharing = bool(self.tunnel or self.server)
        self.server = self.tunnel = None
        self.result.grid_remove()
        if was_sharing and not quiet:
            self.set_status("Stopped sharing. Apps already installed keep working.")
            self._fit_height()

    def close(self):
        if self.tunnel and not messagebox.askokcancel(
                "Stop sharing?", "Your friend's install link stops working when you quit. "
                                 "Apps already installed keep working.", parent=self.root):
            return
        self.stop_sharing(quiet=True)
        self.root.destroy()


def main():
    if IS_WIN:
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except (AttributeError, OSError):
                ctypes.windll.user32.SetProcessDPIAware()
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AdHocShare")
        except Exception:  # noqa: BLE001 - cosmetic only
            pass
    root = tk.Tk(className="adhoc-share")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
