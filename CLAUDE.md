# iPhone Mirror — WiFi Screen Mirroring

macOS desktop app die iPhone-scherm spiegelt en bedient via WiFi. Alternatief voor Apple's iPhone Mirroring die in de EU (nog) niet beschikbaar is.

Runtime target: **macOS 14+, iPhone iOS 17+ (getest op iOS 27).**

---

## Stack

- **Python 3.14** (Homebrew)
- **PyQt6** — GUI + async event loop via `qasync`
- **pymobiledevice3** — device tunneling (v9+ na migratie; v7.8.3 = baseline branch `main`)
- **WebDriverAgent** (WDA) — touch control HTTP server op iPhone (Appium fork)
- **Pillow, requests, keyring** — utilities

---

## Architectuur

```
main.py                     # qasync bootstrap, PyQt event loop
src/
├── main_window.py          # PyQt6 frameless iPhone-vorm UI
├── device_manager.py       # v9 async device discovery + connect
├── tunnel_manager.py       # userspace tunnel start/stop (geen sudo)
├── screen_capture.py       # subprocess MJPEG worker orchestration
├── mjpeg_capture_worker.py # standalone MJPEG stream reader
├── capture_worker.py       # DVT screenshot fallback
├── input_handler.py        # WDA HTTP client (tap/swipe/keys)
├── passcode_store.py       # macOS Keychain voor iPhone passcode
├── wda_auth.py             # WDA bearer token generatie/storage
└── device_models.py        # ProductType → friendly name + screen size
```

### Data flow

1. **Discovery**: userspace tunnel (in-process, no sudo) ontdekt iPhones via bonjour
2. **Connect**: kies device (preferred UDID uit QSettings, of eerste beschikbare)
3. **Developer image mount**: eenmalig per device via CoreDevice
4. **WDA start**: xcuitest launch met env vars (auth token + MJPEG kwaliteit)
5. **Screen capture**: MJPEG stream vanaf iPhone WDA:9100 → subprocess worker → PyQt frame_ready signal
6. **Input**: mouse/keyboard events → WDA HTTP API (port 8100) met Bearer token auth
7. **Display**: frameless iPhone-vormig venster met bezel + rounded corners

---

## Belangrijke besluiten & context

### Waarom pymobiledevice3 v9 (na migratie)
- **`--userspace` tunnel** — geen sudo/LaunchDaemon meer nodig ✅
- **Async API** — modern, betere error handling ✅
- **Betere iOS 27 support** voor DVT/MJPEG ✅
- ❌ CoreDevice `display serve-vnc` NIET beschikbaar op iOS 27 (Apple removed service)
- ❌ CoreDevice HID doet alleen buttons, geen tap/swipe → WDA blijft nodig

### Waarom WDA blijft
- **CoreDevice HID** heeft alleen home/power/volume buttons — geen coordinate taps
- **WDA** doet tap, swipe, drag, pinch, keys via HTTP API
- WDA gebouwd + gesigned met eigen Apple Developer account (Personal Team)
- Custom bundle ID: `com.example.WebDriverAgentRunner.xctrunner`
- Auth token via `Authorization: Bearer <token>` header (source-patched in `FBWebServer.m`)

### Waarom MJPEG > DVT screenshot
- DVT screenshot: ~400-1000ms per frame (WiFi latency) = ~2 FPS
- MJPEG stream: 10-15 FPS met hardware JPEG encoding op iPhone
- MJPEG server op port 9100, config via env vars:
  - `MJPEG_SERVER_SCREENSHOT_QUALITY=55`
  - `MJPEG_SCALING_FACTOR=50`
  - `MJPEG_SERVER_FRAMERATE=12`
- Fallback: DVT subprocess (`capture_worker.py`), dan DVT inline

### Waarom subprocess capture worker
- pymobiledevice3 DVT calls houden GIL vast tijdens ~400ms wait
- In-process = Qt UI stottert
- Aparte Python process communiceert via length-prefixed stdout
- Zelfde geldt voor MJPEG worker

### Waarom `xcuitest` in eigen process group
- SIGTERM op parent kill niet child xcuitest (die blijft draaien op iPhone)
- `start_new_session=True` maakt process group
- `os.killpg(pgid, SIGTERM)` bij app close

---

## Bekende iOS 27 quirks

### WiFi tunnel instabiliteit
- pymobiledevice3 v7.8.3 reverse-engineerde protocol werkt inconsistent op iOS 27 WiFi
- Symptoom: iPhone verdwijnt uit tunneld na WiFi wissel/lock
- **Workaround**: USB kabel er een keer in met "Trust" popup → daarna WiFi weer OK
- v9 verbetert dit via nieuwere QUIC handshake, maar niet 100% opgelost

### CoreDevice display/screencapture unavailable
- iOS 27 blokkeert `com.apple.coredevice.displayservice` voor third-party clients
- Error: `"Apple removed this service, or your iOS version does not support it"`
- Xcode werkt wel — Apple's tools hebben private entitlements
- Betekent: geen 60 FPS hardware VNC voor ons → MJPEG blijft de weg

### Screen size lag
- WDA rapporteert 375×812 of 390×844 (oude iPhone dimensies) i.p.v. echte device size
- Fix: `src/device_models.py` mapt ProductType (bijv. iPhone18,2) → actual size (440×956 voor 17 Pro Max)
- Input handler gebruikt overridden size zodat taps onderin ook werken

### Auto-lock houdt niet met WDA activity
- WDA activity houdt iOS auto-lock timer NIET tegen
- Enige echte fix: iPhone → Instellingen → Weergave → Automatisch slot → Nooit
- Keep-alive ping via `/wda/screen` GET is harmless maar helpt niet tegen lock

---

## Development conventies

### Wat NIET doen
- ❌ `pkill -9` op eigen app processen zonder afsluitritueel — gebruik SIGTERM
- ❌ pymobiledevice3 sync API in de main thread — GIL houdt Qt vast
- ❌ WDA endpoints raden — check `WebDriverAgentLib/Routing/` in de WDA source
- ❌ Root-only paden (LaunchDaemon met `/Volumes/...`) — root heeft geen access tot externe drives
- ❌ `--target=/opt/homebrew/lib/python3.14/site-packages` vergeten bij system pip install (dan komt package in user site-packages en root ziet 'm niet)

### Wat WEL doen
- ✅ Subprocess capture workers voor blocking I/O
- ✅ `--tunnel <udid>` doorgeven aan CLI commando's om device te selecteren
- ✅ Async `_fire_and_forget` voor niet-blocking WDA calls (tap/swipe/etc)
- ✅ Debounce timer voor scroll (voorkomt swipe stacking)
- ✅ Token in Keychain (nooit plaintext in code/config)
- ✅ `os.killpg` voor child processes die eigen process group hebben

---

## Snelle commando's

```bash
# Development
./install.sh              # eenmalige setup (Python, venv, deps)
./run.sh                  # start de app
./uninstall.sh            # cleanup

# Debug
tail -f /tmp/mirror.log                # app log
curl -s http://127.0.0.1:49151/        # tunneld status (v7 baseline)
xcrun devicectl list devices           # Apple's device discovery (referentie)

# WDA rebuild (na iOS update of aanpassing)
cd WebDriverAgent
xcodebuild build-for-testing \
  -project WebDriverAgent.xcodeproj \
  -scheme WebDriverAgentRunner \
  -destination generic/platform=iOS \
  -derivedDataPath /tmp/wda-build \
  DEVELOPMENT_TEAM=<YOUR_TEAM_ID> \
  -allowProvisioningUpdates

# WDA install
python -m pymobiledevice3 apps install \
  /tmp/wda-build/Build/Products/Debug-iphoneos/WebDriverAgentRunner-Runner.app \
  --tunnel <UDID>
```

---

## Testing checklist

Elke wijziging: doorloop dit met echte iPhone 17 Pro Max op zelfde WiFi.

- [ ] `./run.sh` — geen sudo prompt (na v9 migratie)
- [ ] App verbindt binnen 15s met iPhone
- [ ] MJPEG stream start, ≥5 FPS
- [ ] Tap op app icon → app opent
- [ ] Scroll (2-vinger trackpad) → smooth, geen stacking
- [ ] Home button (⌂) → iOS home screen
- [ ] Lock / Unlock met passcode
- [ ] Volume + / −
- [ ] Toetsenbord in tekstveld
- [ ] Cmd+scroll → pinch zoom in Foto's
- [ ] App close → WDA op iPhone stopt (check via Xcode Devices)
- [ ] Positie wordt onthouden bij close+open

---

## Migratie geschiedenis

### Baseline (branch `main`, commit `ce1da49`)
- pymobiledevice3 7.8.3 + LaunchDaemon tunneld + sudoers regel
- WDA custom build met CFBundleDisplayName + auth token patch
- MJPEG capture worker + subprocess isolation
- Frameless iPhone UI, device model detection, passcode Keychain
- ~10 FPS, WiFi instabiel op iOS 27

### v9 migratie (branch `v9-migration`, IN PROGRESS)
- Doel: userspace tunnel (geen sudo, geen LaunchDaemon)
- Async API voor alle pymobiledevice3 interactie
- Behoud: WDA voor input, MJPEG voor video, alle UI features
- Nieuwe features door v9: automatische reconnect via async, robuustere tunnel lifecycle

---

## Externe referenties

- [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) — versie 7.8.3 baseline, 9.35+ target
- [WebDriverAgent (Appium fork)](https://github.com/appium/WebDriverAgent) — WDA source
- [danielpaulus/quicktime_video_hack](https://github.com/danielpaulus/quicktime_video_hack) — USB-only alternative (60 FPS HEVC)
- [Valeria protocol (iOS 18 Mirroring)](https://github.com/doronz88/pymobiledevice3/discussions/1216) — nog niet gereverse-engineerd voor community

---

## Voor Claude sessies

### Documentatie afspraak
- **CLAUDE.md** — blijvende context (dit bestand). Update bij nieuwe conventies / iOS quirks.
- **FINDINGS.md** — technisch logboek van experimenten en ontdekkingen.

### Globale rules geladen via `~/.claude/rules/ecc/`
- `common/` — coding style, testing, security, git workflow, agents, patterns, performance
- `python/` — Python specifieke regels

### Beschikbare agents
- `planner`, `architect`, `python-reviewer`, `code-reviewer`, `security-reviewer`
- `build-error-resolver`, `refactor-cleaner`, `performance-optimizer`
- `silent-failure-hunter`, `doc-updater`, `e2e-runner`

### Snelle slash commands
```
/plan "feature"        → planner agent
/code-review           → code-reviewer agent
/tdd                   → tdd-guide agent
/security-scan         → security-reviewer agent
/build-fix             → build-error-resolver agent
```
