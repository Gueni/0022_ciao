# MatePad SE 11 (AGS6-W09) cleanup

HarmonyOS 2.0.0.156 (Android 10 / API 29).This is a no-root setup.

## What was done (via `platform-tools\adb.exe`)
- **Google:** microG for Huawei (`com.google.android.gms` 0.3.17-hw) + microG Companion were already installed from AppGallery and working (signature spoofing OK, Google account signed in).
- **Installed:** Lawnchair 15 Beta 3 (`app.lawnchair`, set as default home), Aurora Store 4.8.4 (`com.aurora.store`, from F-Droid). APKs are in `apks\`.
- **Uninstalled:** GBox (`com.gbox.android`).
- **Removed for user 0** (`pm uninstall -k --user 0`): hwsearch, browser, browserhomepage, fastapp, hifolder, recsys, android.totemweather, phoneservice, gameassistant, himovie.overseas, music, hicloud, android.thememanager, indiacalendar (all `com.huawei.*`), plus `com.android.printservice.recommendation`.
- **Disabled:** `com.huawei.pcassistant`. (`com.huawei.parentcontrol` is locked by Huawei and can't be removed.)
- Battery-optimisation whitelist: Lawnchair, Aurora, microG.
- Full package list before changes: `packages-before.txt`.

## iPad-style setup
- Also installed: Lawnicons, Breezy Weather (`org.breezyweather`), Etar calendar (`ws.xsoh.etar`), Auxio music (`org.oxycblt.auxio`). APKs in `apks\`.
- Wallpaper: `wallpaper.png` (generated). System dark mode on, animations 0.7x.
- Lawnchair: system icons in iOS shape, 5x6 grid (shows as two panels in portrait), icons 120%, floating dock (6 apps, no labels, no search bar), At a Glance = date + time, Caddy (App-Library-style) drawer at 70% opacity, 11 system apps hidden from the drawer.
- Default home app set via Settings → Apps → Default apps (setting it only by adb got reset by HarmonyOS).
- Bootloader unlock researched: not viable for Kirin 710A on HarmonyOS 2 (see chat).

## Undo
```
platform-tools\adb.exe shell cmd package install-existing <package>
platform-tools\adb.exe shell pm enable com.huawei.pcassistant
platform-tools\adb.exe shell cmd package set-home-activity com.huawei.android.launcher/.unihome.UniHomeLauncher
```
A factory reset also restores everything.

## Keep in mind
- Don't remove: `com.huawei.hwid` (HMS), `com.huawei.appmarket` (it updates microG -hw), `com.huawei.android.hwouc` (system updates), `com.huawei.android.launcher` (gestures/recents).
- Apps with strict Play Integrity (some banks, Google Wallet) may still refuse to run.
