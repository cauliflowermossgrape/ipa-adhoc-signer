#!/usr/bin/env python3
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
Tools rcodesign and cloudflared are downloaded into ./tools on first run if
they aren't already on PATH.
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

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

HERE = Path(__file__).resolve().parent
DATA = HERE / "adhoc_data"  # holds your distribution private key - keep private
TOOLS = HERE / "tools"
CONFIG = DATA / "config.json"
ASC = "https://api.appstoreconnect.apple.com/v1"
# Optional: preset your Issuer ID via env var. It's also remembered in
# adhoc_data/config.json after the first run.
ISSUER_ID = os.environ.get("ADHOC_ISSUER_ID", "")
RCODESIGN_VERSION = "0.29.0"
UDID_RE = re.compile(r"^([0-9a-fA-F]{40}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16})$")
IS_WIN = os.name == "nt"


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
    DATA.mkdir(exist_ok=True)
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
    TOOLS.mkdir(exist_ok=True)
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
    TOOLS.mkdir(exist_ok=True)
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
        cwd=work, capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0)
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
    shutil.copy(out_ipa, HERE / f"{app.stem}-adhoc.ipa")
    shutil.rmtree(work, ignore_errors=True)
    log(f"Signed IPA saved as {app.stem}-adhoc.ipa")

    title = info.get("CFBundleDisplayName") or info.get("CFBundleName") or app.stem
    return out_dir, bundle_identifier, info.get("CFBundleShortVersionString", "1.0"), title


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
    t = html.escape(title)
    (out_dir / "index.html").write_text(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Install {t}</title>
<style>body{{font-family:-apple-system,sans-serif;text-align:center;padding:40px 16px;background:#f5f5f7}}
a.btn{{display:inline-block;margin-top:24px;padding:16px 32px;background:#0071e3;color:#fff;
border-radius:12px;text-decoration:none;font-size:20px}}p{{color:#555}}</style></head><body>
<h1>{t}</h1><p>Version {html.escape(version)}</p><a class="btn" href="{html.escape(link)}">Install</a>
<p>Open this page in <b>Safari</b> on your iPhone, tap Install, then go to your home screen
to watch it download.</p></body></html>""",
                                         encoding="utf-8")


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
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0)
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
class App:
    def __init__(self, root):
        self.root, self.q = root, queue.Queue()
        self.server = self.tunnel = None
        root.title("Ad Hoc Share - send your app to a friend")
        root.geometry("680x600")
        cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}

        f = ttk.Frame(root, padding=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Apple sign-in (App Store Connect API key)",
                  font=("", 11, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="Get these at appstoreconnect.apple.com > Users and Access > Integrations > "
                          "App Store Connect API > + (Access: Admin) > Download. Issuer ID is shown "
                          "above the keys table; picking the AuthKey_XXXX.p8 file fills Key ID for you.",
                  foreground="#666", wraplength=640).grid(row=1, column=0, columnspan=3, sticky="w")

        self.vars = {}
        rows = [("issuer", "Issuer ID", False), ("key_id", "Key ID", False),
                ("p8", "Private key (.p8)", "p8"), ("ipa", "IPA file", "ipa"),
                ("udid", "Friend's UDID", False), ("device", "Device name", False),
                ("bundle", "New bundle ID (optional)", False)]
        for i, (k, label, browse) in enumerate(rows, start=2):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=3)
            default = {"device": "Friend's iPhone", "issuer": ISSUER_ID}.get(k, "")
            v = tk.StringVar(value=cfg.get(k) or default)
            self.vars[k] = v
            ttk.Entry(f, textvariable=v).grid(row=i, column=1, sticky="ew", padx=6)
            if browse:
                ttk.Button(f, text="Browse...", command=partial(self.browse, k, browse)
                           ).grid(row=i, column=2)

        r = len(rows) + 2
        ttk.Label(f, text="UDID: friend connects iPhone to a computer (Finder/iTunes) or visits "
                          "udid.tech in Safari.", foreground="#666").grid(row=r, column=0,
                                                                         columnspan=3, sticky="w")
        self.go = ttk.Button(f, text="Sign & Host", command=self.start)
        self.go.grid(row=r + 1, column=0, columnspan=3, pady=10)

        self.link = tk.StringVar()
        lf = ttk.Frame(f)
        lf.grid(row=r + 2, column=0, columnspan=3, sticky="ew")
        lf.columnconfigure(0, weight=1)
        ttk.Entry(lf, textvariable=self.link, state="readonly").grid(row=0, column=0, sticky="ew")
        ttk.Button(lf, text="Copy link", command=self.copy).grid(row=0, column=1, padx=4)
        ttk.Button(lf, text="Open", command=lambda: self.link.get() and webbrowser.open(self.link.get())
                   ).grid(row=0, column=2)

        self.logbox = scrolledtext.ScrolledText(f, height=14, state="disabled")
        self.logbox.grid(row=r + 3, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        f.rowconfigure(r + 3, weight=1)

        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.pump)

    def browse(self, key, kind):
        types = [("App Store Connect key", "*.p8")] if kind == "p8" else [("iOS app", "*.ipa")]
        p = filedialog.askopenfilename(filetypes=types)
        if p:
            self.vars[key].set(p)
            m = re.match(r"AuthKey_([A-Z0-9]+)\.p8$", Path(p).name)
            if kind == "p8" and m:
                self.vars["key_id"].set(m.group(1))

    def log(self, msg):
        self.q.put(("log", msg))

    def pump(self):
        while not self.q.empty():
            kind, val = self.q.get()
            if kind == "log":
                self.logbox.configure(state="normal")
                self.logbox.insert("end", val + "\n")
                self.logbox.see("end")
                self.logbox.configure(state="disabled")
            elif kind == "link":
                self.link.set(val)
            elif kind == "error":
                messagebox.showerror("Error", val)
                self.go.configure(state="normal")
            elif kind == "done":
                self.go.configure(state="normal")
        self.root.after(100, self.pump)

    def copy(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.link.get())

    def start(self):
        v = {k: s.get().strip() for k, s in self.vars.items()}
        missing = [k for k in ("issuer", "key_id", "p8", "ipa", "udid") if not v[k]]
        if missing:
            return messagebox.showwarning("Missing", "Please fill: " + ", ".join(missing))
        if not UDID_RE.match(v["udid"]):
            return messagebox.showwarning("UDID", "That doesn't look like a valid iPhone UDID.")
        DATA.mkdir(exist_ok=True)
        CONFIG.write_text(json.dumps({k: v[k] for k in ("issuer", "key_id", "p8", "device")}))
        self.go.configure(state="disabled")
        self.stop_hosting()
        threading.Thread(target=self.work, args=(v,), daemon=True).start()

    def work(self, v):
        try:
            api = AppStoreConnect(v["issuer"], v["key_id"], v["p8"])
            self.log("Signing in to App Store Connect...")
            api.get("/devices", {"limit": 1})
            self.log("Signed in.")
            out_dir, bid, ver, title = resign_ipa(v["ipa"], v["bundle"], api, v["udid"],
                                                  v["device"] or "Friend's iPhone", self.log)
            self.server, self.tunnel, url = start_hosting(out_dir, self.log)
            write_install_files(out_dir, url, bid, ver, title)
            self.q.put(("link", url))
            self.log(f"\nDONE. Send your friend this link (open in Safari on the iPhone):\n{url}\n"
                     "Keep this window open until they've installed it. The installed app "
                     "keeps working after you close it, until the profile expires.")
        except Exception as e:  # noqa: BLE001 - surface everything to the user
            self.log(f"ERROR: {e}")
            self.q.put(("error", str(e)))
            return
        self.q.put(("done", None))

    def stop_hosting(self):
        if self.tunnel:
            self.tunnel.kill()
        if self.server:
            self.server.shutdown()
        self.server = self.tunnel = None

    def close(self):
        self.stop_hosting()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
