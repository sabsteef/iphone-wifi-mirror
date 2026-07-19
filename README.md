# iPhone Mirror

Mirror en bedien je iPhone-scherm vanaf macOS over WiFi. Alternatief voor Apple's iPhone Mirroring dat in de EU nog niet beschikbaar is.

## Features

- WiFi screen mirroring via pymobiledevice3 v9 **userspace tunnel — geen sudo, geen LaunchDaemon**
- Auto-reconnect met health monitoring — WiFi drops worden automatisch opgevangen
- Touch control (tap, swipe, scroll, pinch) via WebDriverAgent met Bearer token auth
- Home / Lock / Volume / Unlock (met opgeslagen passcode via macOS Keychain)
- Toetsenbord naar iPhone (tekstvelden, arrow keys, Enter, etc.)
- iPhone-vormig frameless venster met bezel + Dynamic Island
- Device detectie: friendly name + juiste screen size per iPhone model
- MJPEG stream met hardware JPEG encoding (~10 FPS met tunable kwaliteit)

## Prerequisites

- macOS 14+ (Sonoma of nieuwer)
- iPhone met iOS 17+ en Developer Mode aan
- Apple Developer account (voor WebDriverAgent signing)
- Xcode (voor WebDriverAgent bouwen)

## Installatie

### 1. Repo clonen

```bash
git clone https://github.com/<jouw-github>/iPhoneMirroring.git
cd iPhoneMirroring
```

### 2. Automatische install

```bash
./install.sh
```

Dit script:
- Installeert Homebrew (als niet aanwezig)
- Installeert Python 3.14 via Homebrew
- Maakt een virtual environment
- Installeert Python dependencies
- Maakt `run.sh` aan

### 3. iPhone pairen (éénmalig)

```bash
sudo pymobiledevice3 remote pair
```

Volg de instructies op je iPhone.

### 4. WebDriverAgent bouwen

WDA is nodig voor touch control. Je bouwt het met je eigen Apple Developer signing.

```bash
git clone https://github.com/appium/WebDriverAgent.git
cd WebDriverAgent
```

Open `WebDriverAgent.xcodeproj` in Xcode:
1. Selecteer scheme `WebDriverAgentRunner`
2. In Signing & Capabilities: kies je Team, verander Bundle Identifier naar iets uniek (bv. `com.jouwnaam.WebDriverAgentRunner`)
3. Doe hetzelfde voor `IntegrationApp`

Bouw en installeer op je device:

```bash
xcodebuild build-for-testing \
  -project WebDriverAgent.xcodeproj \
  -scheme WebDriverAgentRunner \
  -destination generic/platform=iOS \
  -derivedDataPath /tmp/wda-build

pymobiledevice3 apps install \
  /tmp/wda-build/Build/Products/Debug-iphoneos/WebDriverAgentRunner-Runner.app \
  --tunnel <UDID>
```

**Belangrijk:** onthoud je gekozen bundle ID en pas hem aan in [src/device_manager.py](src/device_manager.py) op `WDA_BUNDLE_ID`.

### 5. App starten

```bash
./run.sh
```

**Geen sudo, geen tunnel service, niks te installeren.** De app opent zijn eigen userspace tunnel in-process. Zodra je iPhone gevonden wordt via usbmux, verbindt hij automatisch en start MJPEG capture.

Als de WiFi tunnel valt: health monitor detecteert het binnen ~6s en herverbindt automatisch met exponential backoff.

## Gebruik

- **Home** (⌂ / Ctrl+H): iPhone home button
- **Lock** (🔒 / Ctrl+L): scherm vergrendelen
- **Unlock** (🔓 / Ctrl+U): scherm wekken + passcode invoeren
- **Vol+/−**: volume
- **↻**: herverbind met device
- **⚙**: settings — passcode + device selectie
- **Drag**: sleep het venster aan de bezel (positie wordt onthouden)
- **Klik in scherm**: tap wordt doorgestuurd naar iPhone
- **Scroll**: 2-vinger trackpad = swipe (met inertia)
- **Cmd + Scroll**: pinch-to-zoom (Foto's, Maps, Safari)
- **Toetsenbord**: type direct in iPhone tekstvelden

## Passcode voor Unlock

Klik ⚙ → vul je 6-cijferige iPhone passcode in → Opslaan.

Wordt bewaard in macOS Keychain per device UDID. Wordt gebruikt door de Unlock knop.

## Troubleshooting

### iPhone niet gevonden

- Zit iPhone op dezelfde WiFi als je Mac?
- Developer Mode aan? (Settings → Privacy & Security → Developer Mode)
- iPhone gepaird? `pymobiledevice3 usbmux list`
- **Bij herhaalde issues**: kabel er even in, "Vertrouw deze computer" popup accepteren, dan kabel eruit — WiFi discovery wordt daarmee gereset.

### Tunnel drops steeds

- Check log — je zou "Tunnel lost" / "Reconnecting" moeten zien
- Als de auto-reconnect na 6 pogingen faalt: klik ↻ voor handmatig
- iOS 27 WiFi kan flaky zijn — pymobiledevice3 v9.36+ heeft hier fixes voor

### WDA connect faalt

- Check dat WDA app op de iPhone geïnstalleerd is
- Bundle ID in `device_manager.py` moet matchen met wat je in Xcode hebt ingesteld
- App start WDA via xcuitest bij eerste connect (dat kan 30s duren)

### FPS laag

- Screen capture draait in subprocess om GIL-contention te vermijden
- Verwachte FPS: 6-12 (afhankelijk van WiFi + Mac CPU)
- Tune in [src/device_manager.py](src/device_manager.py) — de env vars `MJPEG_SERVER_SCREENSHOT_QUALITY`, `MJPEG_SCALING_FACTOR`, `MJPEG_SERVER_FRAMERATE`
- Zie [FINDINGS.md](FINDINGS.md) voor details

## Verwijderen

```bash
./uninstall.sh
```

Verwijdert de venv (en eventuele legacy v7 tunnel service). Geen sudo nodig tenzij v7 residu opgeruimd moet worden.

## Architectuur

- **main.py** — qasync bootstrap, PyQt event loop
- **src/main_window.py** — PyQt6 UI met frameless iPhone-vorm
- **src/tunnel_manager.py** — userspace tunnel + health monitor + auto-reconnect
- **src/device_manager.py** — async device discovery + WDA lifecycle
- **src/screen_capture.py** — MJPEG worker orchestration + DVT fallback
- **src/mjpeg_capture_worker.py** — losstaand MJPEG stream reader
- **src/capture_worker.py** — losstaand DVT screenshot proces (async, v9)
- **src/input_handler.py** — WebDriverAgent HTTP client (touch/keys)
- **src/passcode_store.py** — macOS Keychain wrapper voor passcode
- **src/wda_auth.py** — WDA bearer token generatie/storage
- **src/device_models.py** — ProductType → friendly name + screen size

## Tests

```bash
source .venv/bin/activate
pytest tests/
```

Unit tests dekken TunnelManager (health monitoring, reconnect, backoff) en DeviceManager (device selectie, signal emission). Draaien zonder iPhone — regressie preventie.

## License

MIT (zie LICENSE)

## Roadmap

- [x] Multi-device support (Settings → iPhone kiezen)
- [x] Userspace tunnel (geen sudo, geen LaunchDaemon)
- [x] Auto-reconnect met health monitoring
- [x] Test suite voor regressie preventie
- [ ] `.app` bundle met PyInstaller voor `/Applications`
- [ ] Homebrew Cask voor eenvoudige install
- [ ] Automatische WDA bouw via install script (met Apple Developer prompt)
- [ ] Valeria protocol support (als community die reverse-engineered) voor 60 FPS hardware VNC
