# Ad Hoc Share

Send an iOS app (`.ipa`) to a friend's iPhone **remotely**, signed with your
own paid Apple Developer account, so it keeps working for **about a year**.
No Mac, no Xcode, no TestFlight 90-day expiry.

You fill in a small window, click **Sign & Host**, and get a link. Your friend
opens it in Safari on their iPhone and taps **Install**.

Runs on **Windows**, **Linux** and **macOS**.

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

The first run downloads `rcodesign` and `cloudflared` into `tools/`, unless
they're already on your PATH.

---

## Requirements

| You need | Notes |
|---|---|
| A **paid** Apple Developer Program membership | $99/year. Free Apple IDs can't make Ad Hoc profiles. |
| **Python 3.9+** with tkinter | Windows: install from [python.org](https://www.python.org/downloads/) (tkinter is included). Linux: e.g. `sudo apt install python3 python3-venv python3-tk`. |
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

**Windows:** double-click **`run.bat`**.

**Linux / macOS:**
```bash
chmod +x run.sh
./run.sh
```

The first launch creates a `.venv` and installs dependencies (about a minute).
Then the window opens:

| Field | What to enter |
|---|---|
| Issuer ID | From Step 1 (remembered after the first run) |
| Key ID | Filled in automatically when you pick the `.p8` |
| Private key (.p8) | Your `AuthKey_XXXXXXXXXX.p8` |
| IPA file | The app to send |
| Friend's UDID | From Step 2 |
| Device name | Any label, e.g. "Alex's iPhone". It's only a name in your account. |
| New bundle ID | Leave empty, unless you get a bundle-ID error (see Troubleshooting) |

Click **Sign & Host**. When it finishes you'll see a link like
`https://random-words.trycloudflare.com`, which you can copy and send to your friend.

**Keep the window open until your friend has installed the app.** The link
stops working when you close it, but the installed app keeps working until the
profile expires (about 1 year).

A signed copy of the IPA is also saved next to the script as `<App>-adhoc.ipa`.

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
| **Python was not found** (Windows) | `run.bat` looks for a real Python install and skips the Microsoft Store stub. Install Python from python.org. |
| **The window doesn't open on WSL** | WSL often has no display. Run `run.bat` from Windows instead. |
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

## Files

```
adhoc_share.py      the app
run.bat / run.sh    launchers (create .venv on first run)
requirements.txt    Python dependencies
adhoc_data/         created at runtime, PRIVATE: certificate private key, settings
tools/              created at runtime: rcodesign, cloudflared
```

`adhoc_data/` contains your **distribution certificate's private key**. Keep it
private and back it up if you want to keep reusing the same certificate.

Optional: set the `ADHOC_ISSUER_ID` environment variable to pre-fill the
Issuer ID.

---

## Notes

- Only distribute apps you have the rights to distribute. Ad Hoc distribution
  is intended for testing on a limited number of registered devices.
- This project isn't affiliated with Apple or Cloudflare.
