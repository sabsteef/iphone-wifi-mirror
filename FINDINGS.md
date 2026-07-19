# FINDINGS.md

Technisch logboek voor iPhone Mirror (WiFi edition).

## Kritieke vondsten

### pymobiledevice3 versie-pin (NIET UPGRADEN)
- **Gepin'd op 7.8.3** in requirements.txt
- Vanaf v8.0.0 is `pymobiledevice3.usbmux.list_devices` een async coroutine geworden
- De hele app (device_manager, tunnel-connectie, DVT) gebruikt de synchrone API
- Upgrade naar 8.x/9.x breekt direct met `'coroutine' object is not iterable`

### Screenshot API — correcte imports
- `pymobiledevice3.services.dvt.instruments.screenshot.Screenshot` (v7.8.3)
- NIET `pymobiledevice3.services.screenshot.ScreenshotService` (bestaat niet in 7.x)

### WiFi werkt via dezelfde tunneld als USB
- `pymobiledevice3 remote tunneld` ontdekt devices over zowel USB als WiFi
- Eenmaal getunneld maakt het transport (WiFi/USB) niet meer uit
- Vereist eenmalige WiFi-pairing: `pymobiledevice3 remote pair`

### DVT screenshot bottleneck
- Elke screenshot-call duurt ~400ms over WiFi (Apple protocol-limiet)
- In-process capture: 1.3-1.9s per frame (0.5-0.8 FPS) door GIL-contention met Qt
- Subprocess capture: ~400ms per frame (2.3-3.2 FPS) — GIL elimineerd
- Geen sleep nodig in capture loop — DVT zelf is de rate limiter

## Apparaat info
- iPhone 17 Pro Max, iOS 27.0
- Mac: MacBook Air (sabsteef)
- Python: 3.14.5

## Werkende USB-versie
- Locatie: `~/iPhoneMirroring` (gekloond van Dennisjoch/iPhoneMirroring)
- tunneld draait al, Developer Mode aan, device gepaired
- WebDriverAgent gekloond in `~/iPhoneMirroring/WebDriverAgent/`
- WDA nog niet gebouwd/deployed via Xcode

## WDA via xcuitest (touch control)

### Starten via pymobiledevice3 (NIET via Xcode)
- `pymobiledevice3 developer dvt xcuitest com.sabsteef.WebDriverAgentRunner.xctrunner --tunnel <UDID>`
- Xcode kan het device NIET zien over WiFi ("unavailable") — xcuitest via tunnel is de enige manier
- DeveloperDiskImage moet eerst gemount zijn: `pymobiledevice3 mounter auto-mount --tunnel <UDID>`
- Het xcuitest-proces moet blijven draaien — als het stopt, stopt WDA ook

### WDA bereikbaar via tunnel IPv6 (NIET localhost)
- De tunnel geeft het device een IPv6-adres, bijv. `fd34:fc56:ab79::1`
- WDA URL: `http://[fd34:fc56:ab79::1]:8100`
- `usbmux forward` werkt NIET voor WiFi/Network-devices (geeft BadDevError)
- Het IPv6-adres zit in `rsd.service.address[0]` van het tunneld device-object

### WDA bundle ID
- Origineel: `com.facebook.WebDriverAgentRunner.xctrunner` (niet te registreren bij Apple)
- Gewijzigd naar: `com.sabsteef.WebDriverAgentRunner.xctrunner`
- Build: `xcodebuild build-for-testing -project WebDriverAgent.xcodeproj -scheme WebDriverAgentRunner -destination generic/platform=iOS`
- Install: `pymobiledevice3 apps install /tmp/wda-build/Build/Products/Debug-iphoneos/WebDriverAgentRunner-Runner.app --tunnel <UDID>`

### Bevestigde werking
- Tap: werkt (app openen, navigeren)
- Swipe: werkt (ontgrendelen, scrollen)
- Home button: werkt
- Lock button: werkt
- Volume knoppen: werkt
- Batterij info via WDA: werkt
- Screen scale: 3x, size: 390x844

## FPS over WiFi

### Subprocess capture worker (huidige implementatie)
- Screenshots worden gecaptured in een apart Python-proces (`capture_worker.py`)
- Elimineert GIL-contention tussen pymobiledevice3 en PyQt6
- Resultaat: 2.3-3.2 FPS (vs 0.5-0.8 FPS in-process)
- Perceived latency na tap: ~0.5-1s (was 3-5s)
- Worker heeft retry-logica voor DVT-connectie (tunneld cache is soms stale na mount)
- Bij worker-crash valt app terug op in-process capture (langzamer maar werkend)

### Metingen
| Methode              | Per frame | FPS    |
|----------------------|-----------|--------|
| In-process (met Qt)  | 1.3-1.9s  | ~0.6   |
| Subprocess (worker)  | 310-590ms | 2.3-3.2|
| Isolation benchmark  | 309-591ms | 2.37   |

## Afgerond
- [x] Setup.sh gedraaid, .venv + dependencies geinstalleerd
- [x] WiFi-pairing werkt (van USB-versie overgenomen)
- [x] tunneld vindt iPhone over WiFi
- [x] WebDriverAgent gebouwd, geinstalleerd, en gestart via xcuitest
- [x] Touch control werkt volledig over WiFi
- [x] Subprocess capture worker voor betere FPS
