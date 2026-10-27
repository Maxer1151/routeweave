#!/usr/bin/env bash
set -euo pipefail

# Ручная сборка без Gradle: aapt2 + javac + d8 + zipalign + apksigner.
# Требования: JDK 17, Android SDK с build-tools 35.0.0 и platforms/android-35.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

APP_NAME="VpnSwitcher"
VERSION_NAME="1.2"
VERSION_CODE=3
PKG="com.vpnswitch"
MIN_SDK=29
TARGET_SDK=35
BUILD_TOOLS_VERSION="35.0.0"
PLATFORM_VERSION="android-35"

info() {
    echo "==> $*"
}

err() {
    echo "ОШИБКА: $*" >&2
    exit 1
}

# --- JDK -------------------------------------------------------------------
if [[ -n "${JAVA_HOME:-}" && -x "$JAVA_HOME/bin/javac" ]]; then
    :
elif [[ -x "$HOME/.local/opt/jdk-17/bin/javac" ]]; then
    export JAVA_HOME="$HOME/.local/opt/jdk-17"
elif command -v javac >/dev/null 2>&1; then
    JAVAC_REAL="$(readlink -f "$(command -v javac)")"
    export JAVA_HOME="$(dirname "$(dirname "$JAVAC_REAL")")"
else
    err "JDK 17 не найден. Укажите JAVA_HOME или установите JDK в $HOME/.local/opt/jdk-17"
fi
export PATH="$JAVA_HOME/bin:$PATH"

for tool in javac keytool jar; do
    command -v "$tool" >/dev/null 2>&1 \
        || err "не найден $tool (JAVA_HOME=$JAVA_HOME)"
done
command -v python3 >/dev/null 2>&1 || err "не найден python3 (нужен для манифеста и упаковки dex)"

# --- Android SDK -----------------------------------------------------------
if [[ -n "${SDK:-}" && -d "${SDK}" ]]; then
    :
elif [[ -n "${ANDROID_HOME:-}" && -d "${ANDROID_HOME}" ]]; then
    SDK="$ANDROID_HOME"
elif [[ -n "${ANDROID_SDK_ROOT:-}" && -d "${ANDROID_SDK_ROOT}" ]]; then
    SDK="$ANDROID_SDK_ROOT"
elif [[ -d "$HOME/Android/Sdk" ]]; then
    SDK="$HOME/Android/Sdk"
else
    err "Android SDK не найден. Задайте SDK, ANDROID_HOME или ANDROID_SDK_ROOT"
fi
export SDK

BUILD_TOOLS="$SDK/build-tools/$BUILD_TOOLS_VERSION"
PLATFORM="$SDK/platforms/$PLATFORM_VERSION"
ANDROID_JAR="$PLATFORM/android.jar"

[[ -d "$BUILD_TOOLS" ]] || err "нет build-tools $BUILD_TOOLS_VERSION: $BUILD_TOOLS"
[[ -f "$ANDROID_JAR" ]] || err "нет platforms/$PLATFORM_VERSION/android.jar: $ANDROID_JAR"

for tool in aapt2 d8 apksigner zipalign; do
    [[ -x "$BUILD_TOOLS/$tool" ]] || err "не найден $BUILD_TOOLS/$tool"
done

info "JDK: $JAVA_HOME"
info "SDK: $SDK"
info "build-tools: $BUILD_TOOLS"

# --- Каталоги --------------------------------------------------------------
info "Очистка build/ и подготовка dist/"
rm -rf build
mkdir -p build/gen build/classes build/dex dist keystore
rm -f "dist/$APP_NAME-$VERSION_NAME.apk" "dist/$APP_NAME-$VERSION_NAME.apk.idsig"

# --- Манифест с package ----------------------------------------------------
info "Генерация build/AndroidManifest.xml (package=$PKG)"
python3 - "$ROOT/app/src/main/AndroidManifest.xml" "$ROOT/build/AndroidManifest.xml" "$PKG" <<'PY'
import re
import sys

src_path, out_path, pkg = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src_path, "r", encoding="utf-8") as f:
    text = f.read()

match = re.search(r"<manifest\b([^>]*)>", text)
if not match:
    sys.exit("в исходном манифесте не найден тег <manifest>")
attrs = match.group(1)
if re.search(r"\bpackage\s*=", attrs):
    sys.exit("в исходном манифесте уже задан package — уберите его")

new_tag = "<manifest" + attrs + ' package="' + pkg + '">'
text = text[:match.start()] + new_tag + text[match.end():]
with open(out_path, "w", encoding="utf-8") as f:
    f.write(text)
print("build/AndroidManifest.xml записан")
PY

# --- Ресурсы ---------------------------------------------------------------
info "aapt2 compile"
"$BUILD_TOOLS/aapt2" compile --dir app/src/main/res -o build/res.zip

info "aapt2 link"
"$BUILD_TOOLS/aapt2" link \
    -o build/app-unsigned.apk \
    -I "$ANDROID_JAR" \
    --manifest build/AndroidManifest.xml \
    -R build/res.zip \
    --java build/gen \
    --min-sdk-version "$MIN_SDK" \
    --target-sdk-version "$TARGET_SDK" \
    --version-code "$VERSION_CODE" \
    --version-name "$VERSION_NAME" \
    --auto-add-overlay

# --- Java ------------------------------------------------------------------
info "javac"
find app/src/main/java build/gen -type f -name '*.java' | sort > build/sources.list
[[ -s build/sources.list ]] || err "не найдены исходники .java"
javac -encoding UTF-8 -source 8 -target 8 -Xlint:-options \
    -classpath "$ANDROID_JAR" \
    -d build/classes \
    @build/sources.list

# --- Dex -------------------------------------------------------------------
info "d8"
find build/classes -type f -name '*.class' | sort > build/classes.list
[[ -s build/classes.list ]] || err "не найдены .class после javac"
"$BUILD_TOOLS/d8" \
    --min-api "$MIN_SDK" \
    --lib "$ANDROID_JAR" \
    --output build/dex \
    @build/classes.list

[[ -f build/dex/classes.dex ]] || err "d8 не создал build/dex/classes.dex"

info "Упаковка classes.dex в APK"
cp build/app-unsigned.apk build/app-with-dex.apk
python3 - "$ROOT/build/app-with-dex.apk" "$ROOT/build/dex/classes.dex" <<'PY'
import sys
import zipfile

apk_path, dex_path = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(apk_path, "a", zipfile.ZIP_DEFLATED) as apk:
    if "classes.dex" in apk.namelist():
        sys.exit("в APK уже есть classes.dex — неожиданное состояние")
    apk.write(dex_path, "classes.dex")
    print("classes.dex добавлен, размер:", apk.getinfo("classes.dex").file_size)
PY

# --- Выравнивание и подпись -------------------------------------------------
info "zipalign"
"$BUILD_TOOLS/zipalign" -f 4 build/app-with-dex.apk build/app-aligned.apk

KS="$ROOT/keystore/vpnswitch.keystore"
KS_PASS="${KEYSTORE_PASS:-}"
[[ -n "$KS_PASS" ]] || err "задайте KEYSTORE_PASS — пароль локального keystore $KS"
if [[ ! -f "$KS" ]]; then
    info "Создание локального keystore (файл сохраняется между сборками)"
    keytool -genkeypair \
        -keystore "$KS" \
        -storetype JKS \
        -storepass "$KS_PASS" \
        -keypass "$KS_PASS" \
        -alias vpnswitch \
        -keyalg RSA \
        -keysize 2048 \
        -validity 10000 \
        -dname "CN=VPN Switcher, OU=Dev, O=Local, C=RU"
fi

OUT_APK="$ROOT/dist/$APP_NAME-$VERSION_NAME.apk"
info "apksigner sign"
"$BUILD_TOOLS/apksigner" sign \
    --ks "$KS" \
    --ks-key-alias vpnswitch \
    --ks-pass "pass:$KS_PASS" \
    --key-pass "pass:$KS_PASS" \
    --out "$OUT_APK" \
    build/app-aligned.apk

info "apksigner verify"
"$BUILD_TOOLS/apksigner" verify --verbose --print-certs "$OUT_APK"

info "aapt2 dump badging"
if ! "$BUILD_TOOLS/aapt2" dump badging "$OUT_APK"; then
    echo "ПРЕДУПРЕЖДЕНИЕ: aapt2 dump badging завершился с ошибкой" >&2
fi

info "Готово: $OUT_APK"
ls -lh "$OUT_APK"
