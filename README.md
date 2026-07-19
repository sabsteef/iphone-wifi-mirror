# iPhone Mirror

Mirror en bedien je iPhone-scherm vanaf macOS over WiFi. Alternatief voor Apple's iPhone Mirroring dat in de EU nog niet beschikbaar is.

## Features

- WiFi screen mirroring via pymobiledevice3
- Touch control (tap, swipe, scroll) via WebDriverAgent
- Home / Lock / Volume / Unlock (met opgeslagen passcode via Keychain)
- iPhone-vormig frameless venster met bezel
- Tunnel service auto start/stop met de app (na éénmalige setup)
- Passwordless na installatie (via sudoers regel)

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

Bij de eerste start:
- App detecteert dat de tunnel service niet draait
- Vraagt "Installeer tunnel service?" → klik **Yes**
- macOS vraagt éénmalig je admin wachtwoord
- Installeert LaunchDaemon plist + sudoers regel + pymobiledevice3 in system Python
- Start de tunnel

**Vanaf nu:** tunnel start/stopt automatisch met de app, zonder wachtwoord.

## Gebruik

- **Home** (⌂ / Ctrl+H): iPhone home button
- **Lock** (🔒 / Ctrl+L): scherm vergrendelen
- **Unlock** (🔓 / Ctrl+U): scherm wekken + passcode invoeren
- **Vol+/−**: volume
- **↻**: herverbind met device
- **⚙**: settings — passcode + service beheer
- **Drag**: sleep het venster aan de bezel
- **Klik in scherm**: tap wordt doorgestuurd naar iPhone
- **Scroll/swipe**: doorgestuurd als swipe gesture

## Passcode voor Unlock

Klik ⚙ → vul je 6-cijferige iPhone passcode in → Opslaan.

Wordt bewaard in macOS Keychain per device UDID. Wordt gebruikt door de Unlock knop.

## Troubleshooting

### Tunnel start niet

- Check log: `cat /var/log/iphonemirror-tunneld.log`
- Handmatig testen: `sudo pymobiledevice3 remote tunneld`

### iPhone niet gevonden

- Zit iPhone op dezelfde WiFi?
- Developer Mode aan? (Settings → Privacy & Security → Developer Mode)
- iPhone gepaird? `pymobiledevice3 usbmux list`

### WDA connect faalt

- Check dat WDA app op de iPhone geïnstalleerd is
- Bundle ID in `device_manager.py` moet matchen met wat je in Xcode hebt ingesteld
- App start WDA via xcuitest bij eerste connect (dat kan 30s duren)

### FPS laag

- Screen capture draait in subprocess om GIL-contention te vermijden
- Verwachte FPS: 2-9 (afhankelijk van WiFi + Mac CPU)
- Zie [FINDINGS.md](FINDINGS.md) voor details

## Verwijderen

```bash
./uninstall.sh
```

Verwijdert de tunnel service, sudoers regel en (optioneel) de venv. pymobiledevice3 in system Python blijft staan.

## Architectuur

- **src/main_window.py** — PyQt6 UI met frameless iPhone-vorm
- **src/device_manager.py** — pymobiledevice3 wrapper, tunnel discovery, DVT
- **src/screen_capture.py** — capture thread met subprocess worker
- **src/capture_worker.py** — losstaand DVT capture proces
- **src/input_handler.py** — WebDriverAgent HTTP client (touch/keys)
- **src/tunnel_manager.py** — LaunchDaemon installer, service start/stop
- **src/passcode_store.py** — macOS Keychain wrapper voor passcode

## License

MIT (zie LICENSE)

## Roadmap

- [ ] `.app` bundle met PyInstaller
- [ ] Homebrew Cask voor eenvoudige install
- [ ] Automatische WDA bouw via install script (met Apple Developer prompt)
- [ ] Multi-device support (kies iPhone uit lijst)
