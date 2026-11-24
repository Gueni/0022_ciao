#!/usr/bin/env bash
# Builds TabletPen.apk without Gradle (aapt2 + javac + d8 + apksigner). Usage: bash build.sh [--install]
set -euo pipefail
cd "$(dirname "$0")"
T="$(cygpath -u "$LOCALAPPDATA")/tabletpen-build"
export JAVA_HOME="$(ls -d "$T"/jdk/*/ | head -1)"; export PATH="$JAVA_HOME/bin:$PATH"
BT="$T/sdk/build-tools/34.0.0"; AJ="$T/sdk/platforms/android-29/android.jar"
w() { cygpath -w "$1"; }
rm -rf build; mkdir -p build/classes build/gen assets
cp ../pad.html assets/pad.html
"$BT/aapt2.exe" compile --dir res -o build/res.zip
"$BT/aapt2.exe" link -o build/unsigned.apk -I "$AJ" --manifest AndroidManifest.xml -A assets \
  --java build/gen --min-sdk-version 24 --target-sdk-version 29 build/res.zip
javac -nowarn -Xlint:-options -source 8 -target 8 -encoding UTF-8 -cp "$(w "$AJ")" -d build/classes \
  $(find src build/gen -name '*.java')
cmd //c "$(w "$BT/d8.bat")" --min-api 24 --lib "$(w "$AJ")" --output "$(w build)" $(find build/classes -name '*.class' | while read f; do w "$f"; done)
py -c "import zipfile; z=zipfile.ZipFile('build/unsigned.apk','a'); z.write('build/classes.dex','classes.dex'); z.close()"
"$BT/zipalign.exe" -p -f 4 build/unsigned.apk build/aligned.apk
KS="$T/debug.keystore"
[ -f "$KS" ] || keytool -genkeypair -keystore "$(w "$KS")" -storepass android -keypass android -alias tp \
  -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=TabletPen" >/dev/null 2>&1
cmd //c "$(w "$BT/apksigner.bat")" sign --ks "$(w "$KS")" --ks-pass pass:android --out "$(w ../TabletPen.apk)" "$(w build/aligned.apk)"
echo "built: $(cd ..; pwd)/TabletPen.apk"
if [ "${1:-}" = "--install" ]; then ../../platform-tools/adb.exe install -r ../TabletPen.apk; fi
