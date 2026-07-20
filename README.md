# iPhone WiFi Mirror

Mirror and control your iPhone from macOS over WiFi. An open-source alternative to Apple's **iPhone Mirroring** feature, which — at time of writing — is unavailable in the European Union.

> **No paid Apple Developer subscription needed.** The **free** Apple ID that comes with every iCloud account is enough to sign the on-device runner. All you need is a Mac, Xcode, and your regular Apple ID.

- **Full touch control** (tap, swipe, scroll, pinch-to-zoom, drag, long-press, keyboard input, passcode unlock)
- **No cable required** after initial pairing
- **No sudo, no LaunchDaemons, no background services** — everything runs in-process
- **Auto-reconnect** with health-monitored tunnels: WiFi drops recover automatically
- **iPhone-shaped frameless window** with true device dimensions (widget + Dynamic Island cutout)
- **~9-10 FPS** live MJPEG stream with tunable quality

Tested against **iPhone 17 Pro Max on iOS 27** with **macOS 15+ (Sequoia)**. Any iPhone with iOS 17+ should work — older iOS versions won't because they lack the developer-mode RSD tunnel that this project depends on.

---

## Why this exists

Apple shipped iPhone Mirroring in macOS Sequoia (2024) but disabled it across the European Union pending Digital Markets Act clarity. If you're in the EU with a personal iPhone, you cannot use Apple's version at all.

This project stitches together a working equivalent from three existing open-source pieces:

| Piece | What it does |
|---|---|
| [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) (v9+) | Reverse-engineered device tunnel to the iPhone over WiFi — no jailbreak, no Apple binaries. |
| [WebDriverAgent](https://github.com/appium/WebDriverAgent) (Appium fork) | On-device XCTest runner that exposes an HTTP API for taps, swipes and keys, plus an MJPEG screen stream. |
| [PyQt6 + qasync](https://github.com/CabbageDevelopment/qasync) | Native macOS window that hosts the video stream and forwards mouse/keyboard events. |

The friction is real: you need an **Apple Developer account** (free tier works) to sign WebDriverAgent yourself, because Apple's code-signing model doesn't allow us to ship a pre-signed binary that would run on your phone. See the setup section below — you'll go through it once per iPhone.

---

## What you'll need

- **Mac** running macOS 14+ (Sonoma) with **Xcode 16+** installed
- **iPhone** running iOS 17+ (iOS 27 tested)
- **A regular Apple ID** signed into Xcode → **Settings → Accounts**. Any Apple ID works; the free personal-team tier is all you need.
- **Homebrew** (the installer offers to add it if missing)
- Both devices on the **same WiFi network**

You do **not** need:

- 💰 A paid $99/year Apple Developer Program subscription — the free personal team that comes with every Apple ID works fine
- A jailbroken iPhone
- Any Apple binaries — this uses reverse-engineered protocols only

**Free vs paid — the only real difference:** free-team code signatures expire every **7 days**. That means you rebuild + reinstall the WebDriverAgent runner once a week (~2 minutes). A paid account extends that to 1 year. For personal use, weekly rebuilds are usually fine.

---

## Setup

The setup has three parts. The first two you do **once**. The third — signing WebDriverAgent — needs to be redone every ~7 days if you're on the free Apple Developer tier (that's Apple's certificate expiry, not ours).

### Part 1 — Install the Mac app

**Option A — Prebuilt `.app` (easiest):**

1. Download the latest [`iPhone-Mirror-*.zip`](https://github.com/sabsteef/iphone-wifi-mirror/releases/latest)
2. Unzip and drag `iPhone Mirror.app` into `/Applications`
3. **First launch: right-click → Open** (ad-hoc signed; needed once). If Gatekeeper is stubborn:
   ```bash
   xattr -dr com.apple.quarantine "/Applications/iPhone Mirror.app"
   ```

**Option B — Source install** (for contributors, or if the prebuilt doesn't fit your macOS):

```bash
git clone https://github.com/sabsteef/iphone-wifi-mirror.git
cd iphone-wifi-mirror
./install.sh
```

Installs Homebrew (if missing), Python 3.14, a `.venv/`, and the Python dependencies. Launch later with `./run.sh`. No admin password required.

### Part 2 — Enable Developer Mode & pair your iPhone

Only needed once per iPhone. The order matters — do these steps in sequence.

#### 2a. Turn on Developer Mode (iPhone side)

The Developer Mode menu is **hidden by default** on iOS 17+. It only appears in Settings *after* the iPhone has been talked to by a developer tool at least once. Xcode is the easiest way to trigger this — even just plugging in is enough on many devices, but explicitly connecting with Xcode always works.

1. **Install Xcode** on your Mac (from the App Store) if you haven't already
2. Plug the iPhone into the Mac with a USB cable — a real Apple/MFi cable, not a random no-name one
3. On the iPhone: accept the **"Trust This Computer?"** prompt and enter your passcode
4. On the Mac: open **Xcode → Window → Devices and Simulators** (`⌘⇧2`). Your iPhone should appear in the left sidebar. Wait until it stops showing "Preparing debugger support" (can take a minute the first time)
5. Now on the iPhone: **Settings → Privacy & Security → scroll down → Developer Mode**  
   ⤷ If **Developer Mode** is not there, disconnect the cable, wait 5s, reconnect. It should appear.
6. Toggle **Developer Mode → On**
7. iPhone will ask to restart — restart it
8. After restart, iPhone shows a "Turn On Developer Mode" prompt. Tap **Turn On**, enter your passcode

Verify by going back to **Settings → Privacy & Security → Developer Mode** — the toggle should now be green.

#### 2b. Pair the iPhone with pymobiledevice3 (Mac side)

With the cable still plugged in:

```bash
sudo pymobiledevice3 remote pair
```

You'll get an authentication prompt on the iPhone — accept it and enter your passcode. This creates a **persistent trust record** on both sides so the Mac can talk to the iPhone over WiFi from now on.

Once paired, unplug the USB cable. Everything else runs over WiFi.

> **Both devices must be on the same WiFi network.** If your router has separate 2.4 GHz and 5 GHz SSIDs, join both devices to the *same* one — some mesh routers isolate clients across bands.

#### 2c. Sanity check

```bash
pymobiledevice3 usbmux list
```

Should print at least one entry with your iPhone's UDID. If it doesn't, redo step 2b — the pairing didn't take.

### Part 3 — Build & install WebDriverAgent

This is the part that must be done with **your** Apple ID because iOS refuses to launch code signed by anyone else. Everyone who uses this project needs to do this step once.

**Choose your own bundle ID.** It must be globally unique per Apple ID. A good pattern is `com.yourname.WebDriverAgentRunner` — for example `com.jdoe.WebDriverAgentRunner`. You'll need this exact string later in `WDA_BUNDLE_ID`.

**Clone WebDriverAgent:**

```bash
git clone https://github.com/appium/WebDriverAgent.git
cd WebDriverAgent
open WebDriverAgent.xcodeproj
```

**Add your Apple ID to Xcode first** (once per Mac): **Xcode → Settings → Accounts → +** → Apple ID → sign in with your regular iCloud account. You'll see a "Personal Team" appear underneath — that's what you'll pick as "Team" below.

**In Xcode with the WebDriverAgent project open:**

1. In the left sidebar, click the top **WebDriverAgent** project entry (blue project icon)
2. In the target list next to it, select **WebDriverAgentRunner** (skip `WebDriverAgentRunner_tvOS` unless you're on Apple TV)
3. Open the **Signing & Capabilities** tab (top row of the middle pane)
4. Configure:
   - ✅ **Automatically manage signing**
   - **Team**: pick "*Your Name* (Personal Team)"
   - **Bundle Identifier**: change from `com.facebook.WebDriverAgentRunner` to your own unique one — e.g. `com.jdoe.WebDriverAgentRunner`
5. Now click the target **IntegrationApp** and do the same: pick your Team, change the Bundle Identifier to something unique (e.g. `com.jdoe.WebDriverAgentIntegration`). Won't run at runtime but Xcode refuses to build if it can't sign
6. If Xcode shows a red **"Failed to register bundle identifier"** or **"Provisioning profile requires the Push Notifications capability"** error: check that the bundle IDs really are unique across all your Apple IDs, and that `Automatically manage signing` is ticked so Xcode creates the profile itself

**Build for your device:**

```bash
xcodebuild build-for-testing \
  -project WebDriverAgent.xcodeproj \
  -scheme WebDriverAgentRunner \
  -destination generic/platform=iOS \
  -derivedDataPath /tmp/wda-build \
  -allowProvisioningUpdates
```

**Install onto the iPhone:**

Find your iPhone's UDID first:

```bash
pymobiledevice3 usbmux list
```

Then install (works over WiFi if pairing succeeded in Part 2):

```bash
pymobiledevice3 apps install \
  /tmp/wda-build/Build/Products/Debug-iphoneos/WebDriverAgentRunner-Runner.app \
  --tunnel <YOUR-UDID>
```

**Trust the developer profile on the iPhone:**

The first time you install a build signed by your Apple ID, iOS will refuse to launch it until you approve the developer profile:

- **Settings → General → VPN & Device Management → your Apple ID → Trust**

You only need to do this once per Apple ID per iPhone.

### Part 4 — Point the app at your bundle ID

The mirror app needs to know which bundle ID you chose in Part 3. Either:

- **Env var** (per shell session):
  ```bash
  export WDA_BUNDLE_ID="com.jdoe.WebDriverAgentRunner.xctrunner"
  ```
  Add the line to `~/.zshrc` so it persists.

- **Config file** (persistent, no shell needed) — create `~/.config/iphone-mirror/config.json`:
  ```json
  {
    "wda_bundle_id": "com.jdoe.WebDriverAgentRunner.xctrunner",
    "tap_y_scale": 0.95,
    "tap_x_scale": 1.0
  }
  ```
  The first launch creates this file as a template if missing.

Note the **`.xctrunner`** suffix — that's what Xcode adds to the WebDriverAgentRunner build. If you used `com.jdoe.WebDriverAgentRunner` as bundle ID in Xcode, the runtime bundle is `com.jdoe.WebDriverAgentRunner.xctrunner`.

### Part 5 — Launch

```bash
./run.sh
```

Within ~10 seconds:

- The frameless iPhone-shaped window appears
- Discovery finds your iPhone via mDNS/usbmux
- A userspace tunnel opens (no admin password needed)
- WebDriverAgent launches on the iPhone via XCTest
- Screen mirroring starts at ~9-10 FPS

You're done.

---

## Using it

| Action | How |
|---|---|
| **Tap** | Left-click on the screen |
| **Swipe / drag** | Click and drag |
| **Scroll** | Two-finger trackpad gesture inside the window |
| **Pinch-to-zoom** | ⌘ + scroll (works in Photos, Maps, Safari) |
| **Type** | Just type — text goes to whatever field is focused on the iPhone |
| **Home** | ⌂ button in the app's bottom bar |
| **Lock / Unlock** | 🔒 / 🔓 buttons (Unlock uses saved passcode) |
| **Volume ± ** | +/− buttons |
| **Reconnect** | ↻ button |
| **Settings** | ⚙ button — passcode, device selection |
| **Move window** | Drag by the outer bezel |

### Saving your passcode

Click **⚙ → passcode field → enter your 6-digit iPhone passcode → Save**. Stored in the macOS Keychain per iPhone UDID. The **Unlock** button will wake the phone and type the passcode automatically.

### Custom scroll direction

Two-finger scrolling is configured for reverse-vertical (Windows-style: trackpad down = content moves up), natural horizontal. If you want different behavior, edit `_flush_scroll` in `src/input_handler.py` — the two `finger_dx = …` and `finger_dy = …` lines are marked with a comment.

### Tap alignment

The app compensates for a known WebDriverAgent quirk on newer iPhones where taps drift progressively lower as `y` grows. The compensation is a `TAP_Y_SCALE` env var (default `0.95`). If tapping feels off on your model:

```bash
export TAP_Y_SCALE=0.93   # lower = taps land higher
./run.sh
```

---

## Troubleshooting

### iPhone not found

- Same WiFi as the Mac? (Both connected to the same SSID, not one on 5 GHz and the other on 2.4 GHz on some meshy routers)
- Developer Mode enabled on the phone?
- Try `pymobiledevice3 usbmux list` — does the phone show up over USB?
- **Nuclear reset**: plug in the USB cable, accept "Trust this Computer" again, unplug. This resets Apple's mDNS trust cache which iOS 17+ sometimes needs.

### Tunnel keeps dropping

- iOS 17+ WiFi tunnels are inherently flaky, especially after screen lock. The app auto-reconnects with exponential backoff (up to 6 attempts).
- Persistent failure after screen lock: keep the phone unlocked while using, or set **Settings → Display & Brightness → Auto-Lock → Never**.

### WebDriverAgent connection fails

- Bundle ID mismatch — verify `echo $WDA_BUNDLE_ID` matches what you used in Xcode (+ `.xctrunner`)
- Certificate expired — free-tier Apple certs last 7 days. Rebuild + reinstall.
- Runner not trusted — Settings → General → VPN & Device Management → Trust

### Backspace types glyphs instead of deleting

Older versions had a Unicode-Private-Use-Area bug where WebDriver key codes (``) rendered as ❌ glyphs. Fixed as of this release — control chars go directly (`\x08`, `\r`, `\x09`, etc.).

### Drawing lines are offset from the mouse

Recent WDA builds route drags through `wda/dragfromtoforduration`, which has a linear y-drift on iPhone 17. This project routes drags through the W3C `/actions` endpoint instead — same coord semantics as `/wda/tap`, so lines land where you drag.

### Where are the logs?

Every launch appends to `~/Library/Logs/iPhoneMirror.log` (rotated at ~5 MB). If something goes wrong, `tail -f ~/Library/Logs/iPhoneMirror.log` is where to look.

### Low FPS

MJPEG is I/O bound. If you're getting <5 FPS, check WiFi signal on both ends. Tune JPEG quality with the env vars in `src/device_manager.py`:

```python
"--env", "MJPEG_SERVER_SCREENSHOT_QUALITY=55"  # 1-100, default 55
"--env", "MJPEG_SCALING_FACTOR=50"             # 1-100, default 50
"--env", "MJPEG_SERVER_FRAMERATE=12"           # target FPS, default 12
```

---

## What this project won't do

Being honest about the limits:

- **No 60 FPS mirroring.** MJPEG over WiFi tops out around 10-12 FPS. Apple's iPhone Mirroring uses a private low-latency streaming protocol that isn't accessible without their entitlements. If you need higher FPS, plug in a cable and use [quicktime_video_hack](https://github.com/danielpaulus/quicktime_video_hack) — 60 FPS HEVC over USB, but no touch control.
- **No pixel-perfect pen drawing.** Every touch is one HTTP round-trip through WebDriverAgent (~50-100ms). Fine for taps and gestures; not suitable for signature-style drawing.
- **No always-on background service.** The app is not a menu-bar utility; it opens a window and connects while the window is open.

---

## Architecture

```
main.py                     qasync bootstrap, PyQt event loop
src/
├── main_window.py          PyQt6 frameless iPhone-shaped UI
├── device_manager.py       Async device discovery + WDA lifecycle
├── tunnel_manager.py       Userspace RSD tunnel + health monitor + auto-reconnect
├── tunnel_forwarder.py     TCP forwarder: localhost <-> RSD tunnel
├── screen_capture.py       In-process async MJPEG reader
├── input_handler.py        WebDriverAgent HTTP client (tap/swipe/keys)
├── passcode_store.py       macOS Keychain wrapper for iPhone passcode
├── wda_auth.py             WDA Bearer token generation + storage
└── device_models.py        ProductType → friendly name + screen size
```

Design decisions and reverse-engineering notes live in the commit history and in-code comments.

---

## Contributing

Contributions welcome, especially:

- **Additional iPhone model coverage** in `src/device_models.py` — I only have an iPhone 17 Pro Max to test with
- **Alternative low-latency screen protocols** — if anyone in the pymobiledevice3 community reverse-engineers Apple's Valeria protocol, wiring that in would give real 60 FPS
- **PyInstaller/py2app bundle** so users don't have to deal with the Python side at all
- **CI** — running the test suite on GitHub Actions

Please open an issue first for larger changes. Test suite: `pytest tests/`.

---

## License

MIT — see [LICENSE](LICENSE).

This project is not affiliated with Apple, Facebook, or Appium. WebDriverAgent is © Facebook under the BSD license; pymobiledevice3 is © doronz88 under GPLv3.

---

## Uninstall

```bash
./uninstall.sh
```

Removes the venv. Doesn't touch your WebDriverAgent install on the phone — you can delete that via **Settings → General → VPN & Device Management → your Apple ID → Remove**.
