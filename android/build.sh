#!/usr/bin/env bash
set -euo pipefail
umask 077

# Ручная сборка без Gradle: aapt2 + javac + d8 + zipalign + apksigner.
# Требования: JDK 17, Android SDK с build-tools 35.0.0 и platforms/android-35.
# Единственный источник версии — version.properties рядом с этим скриптом.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

APP_NAME="VpnSwitcher"
PKG="com.vpnswitch"
MIN_SDK=29
TARGET_SDK=35
BUILD_TOOLS_VERSION="35.0.0"
PLATFORM_VERSION="android-35"
VERSION_FILE="$ROOT/version.properties"
BUILD_DIR="$ROOT/build"
GEN_DIR="$BUILD_DIR/gen"
OUT_DIR="$BUILD_DIR/out"
DIST_DIR="$ROOT/dist"
KS_ALIAS="vpnswitch"

info() {
    echo "==> $*"
}

err() {
    echo "ОШИБКА: $*" >&2
    exit 1
}

read_prop() {
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$VERSION_FILE" | tail -n 1
}

# --- Версия ----------------------------------------------------------------
[[ -f "$VERSION_FILE" ]] || err "нет $VERSION_FILE"
VERSION_NAME="$(read_prop VERSION_NAME)"
VERSION_CODE="$(read_prop VERSION_CODE)"
[[ -n "$VERSION_NAME" ]] || err "VERSION_NAME пуст в $VERSION_FILE"
[[ "$VERSION_CODE" =~ ^[0-9]+$ ]] || err "VERSION_CODE должен быть целым числом в $VERSION_FILE"

if [[ "${DEBUG_BUILD:-0}" == "1" ]]; then
    DEBUG_VALUE="true"
else
    DEBUG_VALUE="false"
fi

# --- JDK -------------------------------------------------------------------
if [[ -n "${JAVA_HOME:-}" && -x "$JAVA_HOME/bin/javac" ]]; then
    :
elif [[ -x "$HOME/.local/opt/jdk-17/bin/javac" ]]; then
    export JAVA_HOME="$HOME/.local/opt/jdk-17"
elif command -v javac >/dev/null 2>&1; then
    JAVAC_REAL="$(readlink -f "$(command -v javac)")"
    JAVA_HOME_REAL="$(dirname "$(dirname "$JAVAC_REAL")")"
    export JAVA_HOME="$JAVA_HOME_REAL"
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
info "версия: $VERSION_NAME ($VERSION_CODE)"

# --- Keystore и пароль (проверка до сборки и очистки) ----------------------
KS="${KEYSTORE_PATH:-$ROOT/keystore/vpnswitch.keystore}"
KS_DIR="$(dirname "$KS")"
KS_PASS="${KEYSTORE_PASS:-}"
[[ -n "$KS_PASS" ]] || err "задайте KEYSTORE_PASS — пароль keystore $KS"

PASS_FILE="$(mktemp "${TMPDIR:-/tmp}/vpnswitch-pass.XXXXXX")"
trap 'rm -f "$PASS_FILE"' EXIT
chmod 600 "$PASS_FILE"
# apksigner читает пароли хранилища и ключа из последовательных строк файла.
printf '%s\n%s\n' "$KS_PASS" "$KS_PASS" > "$PASS_FILE"

if [[ ! -f "$KS" ]]; then
    if [[ "${INIT_KEYSTORE:-0}" != "1" ]]; then
        err "keystore не найден: $KS. Создайте его явно: INIT_KEYSTORE=1 KEYSTORE_PASS=... $0 (или задайте KEYSTORE_PATH)"
    fi
    echo "ПРЕДУПРЕЖДЕНИЕ: создаётся новый keystore $KS." >&2
    echo "Сохраните файл и пароль: без них обновления не установятся поверх." >&2
    mkdir -p "$KS_DIR"
    keytool -genkeypair \
        -keystore "$KS" \
        -storetype JKS \
        -storepass:file "$PASS_FILE" \
        -keypass:file "$PASS_FILE" \
        -alias "$KS_ALIAS" \
        -keyalg RSA \
        -keysize 2048 \
        -validity 10000 \
        -dname "CN=VPN Switcher, OU=Dev, O=Local, C=RU"
fi

chmod 600 "$KS" || err "не удалось выставить права 600 на $KS"
if [[ "$KS_DIR" == "$ROOT" || "$KS_DIR" == "$ROOT"/* ]]; then
    chmod 700 "$KS_DIR" || err "не удалось выставить права 700 на $KS_DIR"
fi

info "Проверка keystore и пароля"
keytool -list -keystore "$KS" -alias "$KS_ALIAS" \
    -storepass:file "$PASS_FILE" >/dev/null 2>&1 \
    || err "не удалось открыть $KS: неверный KEYSTORE_PASS или нет alias $KS_ALIAS"

# --- Каталоги --------------------------------------------------------------
info "Очистка build/ и подготовка dist/"
rm -rf "$BUILD_DIR"
mkdir -p "$GEN_DIR" "$BUILD_DIR/classes" "$BUILD_DIR/dex" "$OUT_DIR" "$DIST_DIR"

# --- BuildConfig -----------------------------------------------------------
info "Генерация BuildConfig.java (DEBUG=$DEBUG_VALUE)"
mkdir -p "$GEN_DIR/com/vpnswitch"
cat > "$GEN_DIR/com/vpnswitch/BuildConfig.java" <<EOF
package $PKG;

public final class BuildConfig {
    public static final boolean DEBUG = $DEBUG_VALUE;
    public static final String APPLICATION_ID = "$PKG";
    public static final String VERSION_NAME = "$VERSION_NAME";
    public static final int VERSION_CODE = $VERSION_CODE;

    private BuildConfig() {
    }
}
EOF

# --- Манифест с package ----------------------------------------------------
info "Генерация build/AndroidManifest.xml (package=$PKG)"
python3 - "$ROOT/app/src/main/AndroidManifest.xml" "$BUILD_DIR/AndroidManifest.xml" "$PKG" <<'PY'
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
"$BUILD_TOOLS/aapt2" compile --dir app/src/main/res -o "$BUILD_DIR/res.zip"

info "aapt2 link"
"$BUILD_TOOLS/aapt2" link \
    -o "$BUILD_DIR/app-unsigned.apk" \
    -I "$ANDROID_JAR" \
    --manifest "$BUILD_DIR/AndroidManifest.xml" \
    -R "$BUILD_DIR/res.zip" \
    --java "$GEN_DIR" \
    --min-sdk-version "$MIN_SDK" \
    --target-sdk-version "$TARGET_SDK" \
    --version-code "$VERSION_CODE" \
    --version-name "$VERSION_NAME" \
    --auto-add-overlay

# --- Java ------------------------------------------------------------------
info "javac"
find app/src/main/java "$GEN_DIR" -type f -name '*.java' | sort > "$BUILD_DIR/sources.list"
[[ -s "$BUILD_DIR/sources.list" ]] || err "не найдены исходники .java"
javac -encoding UTF-8 -source 8 -target 8 -Xlint:-options \
    -classpath "$ANDROID_JAR" \
    -d "$BUILD_DIR/classes" \
    @"$BUILD_DIR/sources.list"

# --- Dex -------------------------------------------------------------------
info "d8"
find "$BUILD_DIR/classes" -type f -name '*.class' | sort > "$BUILD_DIR/classes.list"
[[ -s "$BUILD_DIR/classes.list" ]] || err "не найдены .class после javac"
"$BUILD_TOOLS/d8" \
    --min-api "$MIN_SDK" \
    --lib "$ANDROID_JAR" \
    --output "$BUILD_DIR/dex" \
    @"$BUILD_DIR/classes.list"

[[ -f "$BUILD_DIR/dex/classes.dex" ]] || err "d8 не создал build/dex/classes.dex"

info "Упаковка classes.dex в APK"
cp "$BUILD_DIR/app-unsigned.apk" "$BUILD_DIR/app-with-dex.apk"
python3 - "$BUILD_DIR/app-with-dex.apk" "$BUILD_DIR/dex/classes.dex" <<'PY'
import sys
import zipfile

apk_path, dex_path = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(apk_path, "a", zipfile.ZIP_DEFLATED) as apk:
    if "classes.dex" in apk.namelist():
        sys.exit("в APK уже есть classes.dex — неожиданное состояние")
    apk.write(dex_path, "classes.dex")
    print("classes.dex добавлен, размер:", apk.getinfo("classes.dex").file_size)
PY

# --- Выравнивание и подпись (в build/, dist/ не трогаем) -------------------
info "zipalign"
"$BUILD_TOOLS/zipalign" -f 4 "$BUILD_DIR/app-with-dex.apk" "$OUT_DIR/app-aligned.apk"

STAGED_APK="$OUT_DIR/$APP_NAME-$VERSION_NAME.apk"
info "apksigner sign"
"$BUILD_TOOLS/apksigner" sign \
    --ks "$KS" \
    --ks-key-alias "$KS_ALIAS" \
    --ks-pass "file:$PASS_FILE" \
    --key-pass "file:$PASS_FILE" \
    --out "$STAGED_APK" \
    "$OUT_DIR/app-aligned.apk"

info "apksigner verify"
VERIFY_OUT="$("$BUILD_TOOLS/apksigner" verify --verbose --print-certs "$STAGED_APK")"
printf '%s\n' "$VERIFY_OUT"

CERT_LINE="$(printf '%s\n' "$VERIFY_OUT" | grep -F 'certificate SHA-256 digest:' | head -n 1 || true)"
[[ -n "$CERT_LINE" ]] || err "apksigner не вывел SHA-256 сертификата"
CERT_SHA="${CERT_LINE##*certificate SHA-256 digest: }"
info "SHA-256 сертификата: $CERT_SHA"

# --- Публикация (атомарный mv только после успешной проверки) --------------
DIST_APK="$DIST_DIR/$APP_NAME-$VERSION_NAME.apk"
DIST_IDSIG="$DIST_APK.idsig"
STAGED_IDSIG="$STAGED_APK.idsig"

warn_other_apks() {
    local apk
    local -a others=()
    for apk in "$DIST_DIR"/*.apk; do
        [[ -e "$apk" ]] || continue
        [[ "$apk" == "$DIST_APK" ]] && continue
        others+=("$apk")
    done
    if (( ${#others[@]} > 0 )); then
        echo "ПРЕДУПРЕЖДЕНИЕ: в dist/ есть APK других версий:" >&2
        printf '  %s\n' "${others[@]}" >&2
        echo "build.sh их не удаляет. Заархивируйте или удалите вручную, чтобы не перепутать релизы." >&2
    fi
}

info "Публикация $DIST_APK"
mv -f "$STAGED_APK" "$DIST_APK"
if [[ -f "$STAGED_IDSIG" ]]; then
    mv -f "$STAGED_IDSIG" "$DIST_IDSIG"
else
    echo "ПРЕДУПРЕЖДЕНИЕ: apksigner не создал $STAGED_IDSIG" >&2
    rm -f "$DIST_IDSIG"
fi

warn_other_apks

info "aapt2 dump badging"
if ! "$BUILD_TOOLS/aapt2" dump badging "$DIST_APK"; then
    echo "ПРЕДУПРЕЖДЕНИЕ: aapt2 dump badging завершился с ошибкой" >&2
fi

info "Готово: $DIST_APK"
ls -lh "$DIST_APK"
