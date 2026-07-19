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

---

# v9 migratie rapport (2026-07-20)

Baseline `main` branch commit `ce1da49` bevroren als v7.8.3 working state.
Migratie in branch `v9-migration`. Backup in `.backup/`.

## Waarom migreren

**v7.8.3 pijnpunten**:
- Vereist root voor tunneld → LaunchDaemon + sudoers workaround
- Sync API → GIL-contention met Qt (fixte met subprocesses maar leverde extra complexiteit)
- WiFi tunnel op iOS 27 flaky (soms USB nodig om te "primen")
- pip install target-path gedoe voor system Python

**Wat v9 biedt**:
- `UserspaceRsdTunnel` — pure-python netwerkstack, geen root
- Async API die netjes met qasync in de Qt event loop past
- Betere iOS 27 support (recentere reverse engineering)
- `serve-vnc` / CoreDevice display service — helaas gedisabled door Apple op iOS 27

## Onderzocht en verworpen: CoreDevice screen mirror

Getest `pymobiledevice3 developer core-device display serve-vnc` met iOS 27:
- Failt met "Failed to start service. Apple removed this service, or your iOS version does not support it."
- Zelfde error voor `core-device screen-capture screenshot`
- CoreDevice's `hid` service werkt wel voor buttons (home/power/volume) maar NIET voor coordinate taps
- Xcode werkt wel — Apple's tools hebben private entitlements

Conclusie: geen 60 FPS hardware VNC. **MJPEG via WDA blijft de video-pipeline**, WDA blijft nodig voor taps.

## Wel opgelost door v9

1. **Zero sudo, zero LaunchDaemon**
   - `UserspaceRsdTunnel(serial=udid)` async context manager
   - Verifieerd: `os.getuid() == 501` en tunnel opent zonder root
   - LaunchDaemon plist + sudoers regel + system python target-path gedoe: allemaal weg
   - `tunnel_manager.py` van 244 regels → 79 regels

2. **Async architectuur**
   - `qasync.QEventLoop` bindt asyncio aan Qt event loop
   - `main.py` bootstrap start `MainWindow.start_async()` op de loop
   - `DeviceManager` is een `QObject` + async methods; discovery is een asyncio task ipv QTimer
   - Cleanup via `async_close()` triggerd door SIGTERM signal handler

3. **Simpelere install/uninstall**
   - `install.sh`: geen sudo prompts meer
   - `uninstall.sh`: alleen legacy v7 opruiming (als aanwezig)

## Compatibiliteitsopmerkingen v9

**API breekt v7 patterns**:
- `pymobiledevice3.usbmux.list_devices` → coroutine (moet `await` krijgen)
- `pymobiledevice3.lockdown.create_using_usbmux` → coroutine
- Alle CLI subcommands zijn onder `async_command` decorator

**Nog niet geport naar v9 (fallback)**:
- `src/capture_worker.py` — sync DVT screenshot subprocess. Werkt nog want draait als losstaand proces met eigen v7-stijl imports (v9 API is backward compatible voor deze klassen).
- Als v9 die klassen ooit removed moeten we hem herschrijven met async DVT.

**MJPEG worker onaangeraakt**:
- `src/mjpeg_capture_worker.py` gebruikt geen pymobiledevice3, alleen socket + stdout
- Werkt met elke pymobiledevice3 versie

## Modules overzicht na migratie

| Module | v7 regels | v9 regels | Verandering |
|--------|-----------|-----------|-------------|
| tunnel_manager | 244 | 79 | -68% (LaunchDaemon weg) |
| device_manager | 470 | 232 | -50% (threading weg, async model) |
| screen_capture | 254 | 214 | -16% (compat updates) |
| main | 91 | 100 | +10% (qasync bootstrap) |
| main_window | 880 | ~810 | -8% (service dialog weg, async close) |
| input_handler | 583 | 583 | 0 (puur HTTP, geen pymd3) |

## Testresultaten (smoke)

- ✅ App start als user 501, geen sudo prompt
- ✅ qasync event loop draait, asyncio tasks werken
- ✅ Discovery loop poll usbmux zonder crash
- ✅ Graceful shutdown via SIGTERM (signal handler → async_close)
- ✅ Alle imports laden onder v9
- ⏳ End-to-end met iPhone: niet getest (iPhone niet in tunneld tijdens migratie)
- ⏳ WiFi tunnel stabiliteit op iOS 27: te bepalen

## Wat morgen te doen

1. Kabel er even in met "Vertrouw deze computer" om iPhone bereikbaar te maken
2. `./run.sh` starten
3. Verifieren:
   - Userspace tunnel opent zonder sudo
   - MJPEG @ ~10 FPS
   - WDA touch werkt
4. Testen of WiFi (kabel eruit) stabieler is dan onder v7
5. Als stabiel: PR merge v9-migration → main

## Openstaande punten voor later

- **capture_worker.py** async porten voor volledige v9 consistentie
- **Auto-reconnect** in `TunnelManager` bij drops (v9 laat tunnels heropenen)
- **Valeria protocol** — als community het reverse-engineered geeft dat 60 FPS mirror en Apple's iPhone Mirroring feature-parity
- **PyInstaller .app bundle** — nu makkelijker want geen sudo/LaunchDaemon post-install
