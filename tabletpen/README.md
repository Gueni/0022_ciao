# TabletPen

Use the MatePad and its stylus as a pressure-sensitive pen on **any Windows PC**.
It works in Paint, OneNote, Whiteboard, Krita and any other app that accepts pen input.

## Use
- **PC:** double-click `TabletPen.exe` once. It installs itself for your user, with no admin rights or Python needed:
  - it copies itself to `%LOCALAPPDATA%\Programs\TabletPen`
  - adds a Start menu entry
  - starts with Windows from then on, quietly in the system tray

  Running a newer `TabletPen.exe` the same way updates it. To keep it portable instead, run it with `--portable`.
  A copy is on the tablet in `Download/TabletPen/`, so you can bring it to another PC.
- **Tablet:** open the **TabletPen** app.

There is nothing to pick and no code to type. The two find each other over whatever is available:

| Link | Needs | Live PC preview on the tablet |
|---|---|---|
| USB | the cable. Plugging in also opens the app on the tablet | sharp, live |
| Wi-Fi | both on the same network (no internet needed) | sharp, live |
| Bluetooth | Bluetooth on, on both | small changes sharp; after big changes a quick gray frame, then sharp about 1 s after the screen settles |

USB is preferred, then Wi-Fi, then Bluetooth. Tap **link** on the tablet to see the connections or switch between them.

- **New PC:** the first time a PC sees the tablet over Wi-Fi or Bluetooth, it asks once: *"Allow this tablet to draw on this PC?"*.
  Plugging in USB counts as approval.
- **New PC over USB:** the tablet asks once to *"Allow USB debugging"*.
  Some PCs may also need the Huawei USB driver. Wi-Fi and Bluetooth need nothing.
- **Tray menu:** overall status plus a line each for USB, Wi-Fi and Bluetooth saying what's wrong (for example a missing driver, Bluetooth off, or nothing found on the network). It also has **Connect to tablet by IP…** for networks that block discovery (the tablet shows its IP under *link*), **Turn off media screen**, **Open screenshots folder**, **Open log**, **Start with Windows** (on by default) and Quit.

## Sidebar
**link · remote · paint · media · shot · settings.** Pen and finger always draw: everything you do with them happens on the PC screen, and the tablet shows it live.

## Remote mode (🖱 in the sidebar)
Use the tablet as a remote for the PC from the couch:
- **Touchpad:** one finger moves the pointer, tap = click (two taps = double-click), two-finger tap = right-click, two fingers scroll, press & hold still = grab, then slide to drag.
- **⌨ Type:** opens the tablet keyboard. Whatever you type goes straight to the PC. Tap anywhere outside it to close it.
- **Buttons:**
  - Keys: Esc, Tab, Enter, arrows and Page Up/Down.
  - Apps and windows: Start, switch app, all windows, show desktop, close app, copy and paste.
  - Media: previous, play/pause, next, volume and mute, F11.
- The pen still draws while remote mode is on.

## Paint (🎨 in the sidebar)
Opens Windows Paint on the PC (maximized, fitted to the window) and shows only its canvas on the tablet. Draw with the pen (pressure works) or a finger; it happens live in Paint.
- **Toolbar:** pencil, brush, eraser, fill, text, color picker, select; line, rectangle, oval, triangle, arrow, star and heart; size; Paint's 20 colors; undo, redo, clear and save.
- The toolbar presses Paint's own buttons through Windows UI Automation, so Paint shows exactly what you picked.

## Media (🎵 in the sidebar)
Music and videos from the couch, while a game or film keeps running on the PC or TV.
- **Now playing:** every player Windows knows about (Spotify, browsers, media apps), with cover art, title, a seekable progress bar, previous / play-pause / next, and the PC volume.
- **YouTube / Spotify on the tablet:** each opens on a hidden second screen that only the tablet shows. Tap, scroll and type on the tablet to use the real app. The PC's own screen is never touched, and the game keeps focus the whole time.
  - **Spotify:** your own Spotify app. The first time, TabletPen restarts it once so it can be controlled.
  - **YouTube:** a dedicated Brave window (Edge or Chrome if Brave isn't installed). Tap **Sign in** once on the tablet and it's your account and channel. It can't be your everyday Brave window, because Chromium refuses remote control of a browser's main profile; that's a deliberate security rule.
  - **App bar:** ◀ Back, switch YouTube / Spotify, ⌨ Type, play/pause and volume, **Turn off** (removes the hidden screen) and ✕ Close.
- **Hidden screen:** a 1920×1200 virtual monitor from the Parsec Virtual Display Driver, installed once with admin rights. TabletPen plugs it in to the right of your real screens without changing their setup (TV, duplicate and so on), and removes it on **Turn off**, from the tray, or when TabletPen quits.
- Input goes into the apps through Chromium's local DevTools channel (127.0.0.1 only), not as Windows mouse or keyboard input. That's why the game never loses focus.
- **Over Bluetooth:** Now playing works, but showing the hidden screen needs USB or Wi-Fi.

## Sidebar extras
- **📸 shot:** one tap saves a full-resolution screenshot of the PC screen (or the mapped window) to the PC's `Pictures\Screenshots`. A copy also goes to the tablet's gallery (`Pictures/TabletPen`), which you can turn off in settings.
- **⚙ settings:** pointer speed, scroll speed, natural scrolling, the screenshot copy, and all the gesture tips.

## How it works
The PC is always the side that connects, so Windows Firewall never asks anything.
- **Wi-Fi:** the PC broadcasts `TABLETPEN?` on UDP 8766. The app answers and the PC connects to TCP 8767 on the tablet.
- **USB:** the PC runs a bundled adb and does `adb forward` to the tablet's 8767.
- **Bluetooth:** the tablet advertises a BLE GATT service and the PC connects as the central, using bleak.
  The roles are this way round because some Intel adapters, this PC's included, refuse to advertise as a connectable peripheral.
- **Preview:** only the part of the PC screen that changed is sent, at the tablet's exact pixel size. The tablet confirms each patch before the PC sends the next one, so frames never pile up and taps are never queued behind them.
- When the tablet app is in the background, the preview pauses, so the PC uses almost no CPU (about 0.5%).
- The tablet sends a heartbeat every 2 seconds, and the PC drops any link that goes quiet for 7 seconds.
- Trusted tablets are stored in `%APPDATA%\TabletPen\trusted.json`. The log is in `log.txt` in the same folder.

## Files
- **PC:**
  - `main.py`: tray app.
  - `core.py`: approval, modes, pen dispatch, preview.
  - `media.py`: now playing.
  - `appscreen.py`: the hidden media screen, with YouTube and Spotify over DevTools.
  - `vdd.py`: Parsec virtual monitor and display layout.
  - `paint.py`: Paint over UI Automation.
  - `net_links.py`: USB and Wi-Fi.
  - `ble_link.py`: Bluetooth.
  - `pen.py`: Windows pen and touch injection.
- **Tablet:** `android/` (native links: `Net.java`, `Ble.java`, `Hub.java`), which hosts `pad.html` (the drawing UI).

## Build
- **Tablet app:** `bash android/build.sh --install`. It uses no Gradle. The JDK and Android SDK it needs are in `%LOCALAPPDATA%\tabletpen-build`.
- **PC exe:** PyInstaller one-file build of `main.py`, bundling `icon.png` and `platform-tools/adb*`. The exact command is in the memory notes.
- **Run from source:** `start.bat`, or `py main.py --console`. Set `TABLETPEN_DEBUG=1` to log pen packets.
