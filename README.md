# Ad Hoc Share: Sign and Install IPA Files on iPhone Remotely (No Mac)

Send an iOS app (`.ipa`) to a friend's iPhone **remotely**, signed with your
own paid Apple Developer account, so it keeps working for **about a year**.
No Mac, no Xcode, no TestFlight 90-day expiry.

You fill in a small window, click **Sign and share**, and get a link. Your
friend opens it in Safari on their iPhone and taps **Install**.

Runs on **Windows** and **Linux**. macOS should work from source but is untested.

## Download

Get the latest build from the **[Releases](../../releases)** page:

| OS | File | How to run |
|---|---|---|
| Windows 10/11 | `AdHocShare-windows-x64.exe` | Double-click it. If SmartScreen warns, click **More info → Run anyway** (the exe isn't code-signed). |
| Linux x86_64 | `AdHocShare-linux-x86_64` | `chmod +x AdHocShare-linux-x86_64 && ./AdHocShare-linux-x86_64` |

No Python needed for these. To run from source instead, see [Run from source](#run-from-source).

---

## What it does

1. Signs in to your developer account with an **App Store Connect API key**.
2. Registers your friend's iPhone (UDID) and the app's bundle ID.
3. Creates (or reuses) an **Apple Distribution** certificate.
4. Creates an **Ad Hoc provisioning profile**, valid for 12 months, for the app
   and for every app extension inside it (widgets, share/Safari extensions and so on).
5. Re-signs the whole IPA with [rcodesign](https://github.com/indygreg/apple-platform-rs),
   which runs on Windows and Linux, so no Mac is needed.
6. Hosts the IPA and an install page on a free public HTTPS link via a
   [Cloudflare quick tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/)
   (`https://something.trycloudflare.com`, no Cloudflare account needed).

The first run downloads `rcodesign` and `cloudflared` into the
[data folder](#where-your-data-is-stored), unless they're already on your PATH.

---

## Requirements

| You need | Notes |
|---|---|
| A **paid** Apple Developer Program membership | $99/year. Free Apple IDs can't make Ad Hoc profiles. |
| Python 3.9+ with tkinter, **only if running from source** | Windows: [python.org](https://www.python.org/downloads/) (tkinter included). Linux: `sudo apt install python3 python3-venv python3-tk` (Fedora: `python3-tkinter`, Arch: `tk`). |
| An App Store Connect **API key** (`.p8`) | See [Step 1](#step-1--create-an-app-store-connect-api-key). |
| Your friend's **UDID** | See [Step 2](#step-2--get-your-friends-udid). |
| A **decrypted** IPA | IPAs pulled straight from the App Store are encrypted and will crash. |

---

## Step 1 - Create an App Store Connect API key

This is how the tool logs in to your developer account. Apple doesn't allow
scripts to log in with your Apple ID password and 2FA, so an API key is the
official way. You only do this once.

1. Go to **[appstoreconnect.apple.com](https://appstoreconnect.apple.com)** and sign in with your developer Apple ID.
2. Open **Users and Access**, then the **Integrations** tab, then **App Store Connect API** (left sidebar), then **Team Keys**.
   - The first time, you may see **Request Access**. Only the *Account Holder*
     can click it. Accept the terms and access is granted right away.
3. Click **+** (Generate API Key).
   - **Name:** anything, e.g. `adhoc-share`
   - **Access:** **Admin**. *App Manager* also works.
4. Click **Generate**, then **Download** next to the new key.
   - You get a file named `AuthKey_XXXXXXXXXX.p8`.
   - ⚠️ **Apple only lets you download it once.** Keep it somewhere safe and private.
5. Note these two values from the same page:
   - **Issuer ID**: the long ID shown *above* the keys table, looking like
     `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`. It's the same for all keys on your account.
   - **Key ID**: the 10-character ID in the key's row. It's also the part
     of the filename after `AuthKey_`, and the tool fills it in automatically
     when you pick the file.

> The `.p8` file is effectively a password to your developer account.
> Don't share it and don't commit it. It's already in `.gitignore`.
> If it leaks, click **Revoke** on that page.

---

## Step 2 - Get your friend's UDID

The UDID is the iPhone's unique ID (looks like `00008110-001A2B3C4D5E6F70`
or 40 hex characters). Ad Hoc apps only install on devices whose UDID is in
the profile, so you need it before signing.

Your friend can get it in any of these ways:

**Option A - On the iPhone (easiest, fully remote)**
1. Open **[udid.tech](https://udid.tech)** (or any similar UDID site) in **Safari**.
2. Follow the prompts to download a small profile, then go to **Settings → Profile Downloaded → Install**.
3. The site shows the UDID. Copy it and send it to you.
4. Optional: remove the profile afterwards in **Settings → General → VPN & Device Management**.

**Option B - Mac**
1. Plug the iPhone into the Mac and open **Finder**, then select the iPhone in the sidebar.
2. Click the gray text under the phone's name (model, storage) until it shows **UDID**.
3. Right-click, **Copy UDID**.

**Option C - Windows**
1. Plug the iPhone in and open **iTunes** (or **Apple Devices**), then click the phone icon.
2. In the summary, click **Serial Number** until it changes to **UDID**.
3. Copy it (Ctrl+C).

> The UDID isn't shown anywhere in the iPhone's Settings app. You need
> one of the methods above.

You can register up to **100 iPhones per membership year**. Removing a device
only frees its slot when your membership renews.

---

## Step 3 - Run it

Open the app (see [Download](#download)) and fill in the window:

| Field | What to enter |
|---|---|
| Issuer ID | From Step 1 (remembered after the first run) |
| API key file | Your `AuthKey_XXXXXXXXXX.p8`. Click **Choose…**. |
| Key ID | Filled in automatically from the file name |
| IPA file | The app to send. The window shows its name, version, bundle ID and minimum iOS. |
| New bundle ID | Leave empty, unless Apple says the bundle ID is taken (see Troubleshooting) |
| UDID | From Step 2. The window tells you if it looks valid. |
| Device name | Any label, e.g. "Alex's iPhone". It's only a name in your account. |

Click **Sign and share**. Progress shows at the bottom of the window, and
**Show details** opens the full log. When it finishes, a green panel shows a
link like `https://random-words.trycloudflare.com` with **Copy link** and
**Open** buttons. Send that link to your friend.

**Keep the window open until your friend has installed the app.** The link
stops working when you close the window or click **Stop sharing**, but the
installed app keeps working until the profile expires (about 1 year).

A signed copy of the IPA is saved next to the original as `<name>-adhoc.ipa`.

### Adding another friend later

Run it again with the new UDID. Every profile includes **all** devices
registered on your account, so the new link works for everyone. Earlier
friends don't need to reinstall unless their profile is about to expire.

---

## For your friend: installing

1. Open the link **in Safari** (not Chrome or an in-app browser).
2. Tap **Install**, then **Install** again in the popup.
3. Go to the home screen and watch the app download.
4. Open it. No "Trust developer" step and no Developer Mode are needed for
   Ad Hoc apps.

The iPhone must run at least the app's minimum iOS version (`MinimumOSVersion`
in the app's `Info.plist`).

---

## Troubleshooting

| Problem | Cause / fix |
|---|---|
| **"Unable to Install"** / **"integrity could not be verified"** | The device's UDID isn't in the profile. Check the UDID, run it again, and use the **new** link. Also delete any older copy of the app first. |
| **App closes immediately on launch** | Most often the IPA is still **encrypted** (pulled from the App Store), so use a decrypted IPA. Otherwise get the crash log: iPhone **Settings → Privacy & Security → Analytics & Improvements → Analytics Data → `AppName-….ips`**. |
| **Bundle ID error** ("already exists" / "not available") | The ID belongs to another developer team. Enter your own in **New bundle ID**, e.g. `com.yourname.appname`. Extensions are renamed to match automatically. |
| **Certificate limit reached** | Apple allows only a few distribution certificates. Revoke an unused one at developer.apple.com → Certificates. Apps signed with a revoked certificate stop launching. |
| **Windows SmartScreen blocks the exe** | The exe isn't code-signed. Click **More info → Run anyway**, or run from source. |
| **Python was not found** (Windows, from source) | `run.bat` looks for a real Python install and skips the Microsoft Store stub. Install Python from python.org. |
| **"tkinter is missing"** (Linux, from source) | `sudo apt install python3-tk` (Fedora: `python3-tkinter`, Arch: `tk`). |
| **The window doesn't open on WSL** | WSL often has no display. Use the Windows exe or `run.bat` instead. |
| **Link doesn't load** | The window must stay open, and each run creates a new link, so send the latest one. |

---

## How long does it last?

- **Ad Hoc profile:** 12 months from creation.
- **Distribution certificate:** 12 months. The tool makes a fresh one if the
  stored one has less than about 11 months left, so a new profile gets the full year.
- **Developer membership:** if it lapses, or the certificate is revoked, the
  installed apps stop opening.

To go past a year, run the tool again and have your friends reinstall from the new link.

---

## Where your data is stored

Settings, your **distribution certificate's private key**, and the downloaded
tools live in a per-user folder, never next to the app:

| OS | Folder |
|---|---|
| Windows | `%APPDATA%\AdHocShare` |
| Linux | `~/.local/share/adhoc-share` (or `$XDG_DATA_HOME/adhoc-share`) |
| macOS | `~/Library/Application Support/AdHocShare` |

Keep this folder private, and back it up if you want to keep reusing the same
certificate. Set the `ADHOC_DATA_DIR` environment variable to use a different
folder.

If you used an older version that stored data in `adhoc_data/` next to the
script, running from source copies it into the new folder automatically. Your
existing certificate keeps working.

Other environment variables:
- `ADHOC_ISSUER_ID` pre-fills the Issuer ID.
- `ADHOC_THEME=light` or `dark` overrides the system light/dark setting.

---

## Run from source

**Windows:** double-click **`run.bat`**.

**Linux / macOS:**
```bash
chmod +x run.sh
./run.sh
```

The first launch creates a virtual environment and installs dependencies
(about a minute). Later launches open straight away.

---

## Building the executables

```bash
python -m pip install -r requirements.txt -r requirements-build.txt
python build.py
```

The executable lands in `dist/`, named for the OS you built on. PyInstaller
can't cross-compile, so build the Windows exe on Windows and the Linux binary on Linux.

On Linux, build with a Python whose Tk supports Xft (distro Python with
`python3-tk`, or conda-forge `tk=*=xft*`). Otherwise the app renders in blurry
bitmap fonts.

### Releasing

Push a version tag and GitHub Actions builds both executables and attaches
them to a **draft release** ([`.github/workflows/release.yml`](.github/workflows/release.yml)):

```bash
git tag v1.0.0
git push origin v1.0.0
```

Review the draft on the Releases page, then publish it.

### Project layout

```
adhoc_share.py          the app (single file)
run.bat / run.sh        run from source (create a venv on first run)
build.py                build a single-file executable with PyInstaller
assets/                 app icon (regenerate with scripts/make_icon.py)
.github/workflows/      release build
```

---

## Notes

- Only distribute apps you have the rights to distribute. Ad Hoc distribution
  is intended for testing on a limited number of registered devices.
- This project isn't affiliated with Apple or Cloudflare.

---

## License

Copyright (C) 2026 cauliflowermossgrape

This program is free software: you can redistribute it and/or modify it under
the terms of the **GNU General Public License v3.0** as published by the Free
Software Foundation. It is distributed WITHOUT ANY WARRANTY. See
[`LICENSE`](LICENSE) for the full text.
