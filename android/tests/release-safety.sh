#!/usr/bin/env bash
# Проверка release-безопасности build.sh: успешная сборка, затем две неудачные
# попытки (неверный и отсутствующий пароль). Прошлый APK не должен измениться.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANDROID_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TMP_BASE="${RELEASE_SAFETY_TMP:-${TMPDIR:-/tmp}}"
BUILD_TOOLS_VERSION="35.0.0"

info() {
    echo "==> $*"
}

KEEP_TMP=0

err() {
    echo "ОШИБКА: $*" >&2
    KEEP_TMP=1
    exit 1
}

cleanup() {
    if [[ "$KEEP_TMP" == "1" ]]; then
        echo "Песочница сохранена для разбора: $WORK" >&2
    else
        rm -rf "$WORK"
    fi
}

for tool in rsync sha256sum sed grep awk mktemp; do
    command -v "$tool" >/dev/null 2>&1 || err "не найден $tool"
done

SDK="${SDK:-${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Android/Sdk}}}"
export SDK
AAPT2="$SDK/build-tools/$BUILD_TOOLS_VERSION/aapt2"
APKSIGNER="$SDK/build-tools/$BUILD_TOOLS_VERSION/apksigner"
[[ -x "$AAPT2" ]] || err "не найден $AAPT2 (задайте SDK или ANDROID_HOME)"
[[ -x "$APKSIGNER" ]] || err "не найден $APKSIGNER (задайте SDK или ANDROID_HOME)"

VERSION_FILE="$ANDROID_DIR/version.properties"
[[ -f "$VERSION_FILE" ]] || err "нет $VERSION_FILE"

read_prop() {
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$VERSION_FILE" | tail -n 1
}

VERSION_NAME="$(read_prop VERSION_NAME)"
VERSION_CODE="$(read_prop VERSION_CODE)"
[[ -n "$VERSION_NAME" ]] || err "VERSION_NAME пуст в $VERSION_FILE"
[[ "$VERSION_CODE" =~ ^[0-9]+$ ]] || err "VERSION_CODE должен быть целым числом в $VERSION_FILE"

mkdir -p "$TMP_BASE"
WORK="$(mktemp -d "$TMP_BASE/routeweave-release-safety.XXXXXX")"
trap cleanup EXIT
info "Песочница: $WORK"

COPY="$WORK/android"
mkdir -p "$COPY"
rsync -a --exclude=/build/ --exclude=/dist/ --exclude=/keystore/ \
    "$ANDROID_DIR/" "$COPY/"

KS="$WORK/keystore/vpnswitch.keystore"
PASS_OK="release-safety-pass-$$"
APK="$COPY/dist/VpnSwitcher-$VERSION_NAME.apk"

info "Успешная сборка с временным keystore"
if ! KEYSTORE_PATH="$KS" KEYSTORE_PASS="$PASS_OK" INIT_KEYSTORE=1 \
        "$COPY/build.sh" >"$WORK/build-ok.log" 2>&1; then
    tail -n 40 "$WORK/build-ok.log" >&2
    err "успешная сборка не удалась"
fi

[[ -f "$APK" ]] || err "после успешной сборки нет $APK"
[[ -f "$APK.idsig" ]] || err "после успешной сборки нет $APK.idsig"

SHA_BEFORE="$(sha256sum "$APK" | awk '{print $1}')"
IDSIG_SHA_BEFORE="$(sha256sum "$APK.idsig" | awk '{print $1}')"

BADGING="$("$AAPT2" dump badging "$APK")"
BADGING_LINE="${BADGING%%$'\n'*}"
grep -Fq "versionCode='$VERSION_CODE'" <<<"$BADGING_LINE" \
    || err "в badging нет versionCode='$VERSION_CODE': $BADGING_LINE"
grep -Fq "versionName='$VERSION_NAME'" <<<"$BADGING_LINE" \
    || err "в badging нет versionName='$VERSION_NAME': $BADGING_LINE"
info "badging: $BADGING_LINE"

"$APKSIGNER" verify "$APK" >"$WORK/verify.log" 2>&1 \
    || { tail -n 20 "$WORK/verify.log" >&2; err "apksigner verify не прошёл"; }

run_expect_failure() {
    local label="$1"
    shift
    local log="$WORK/build-$label.log"
    local rc=0
    if "$@" >"$log" 2>&1; then
        rc=0
    else
        rc=$?
    fi
    if (( rc == 0 )); then
        err "сборка с $label неожиданно завершилась успешно"
    fi
    info "Ожидаемое падение ($label): exit $rc"
}

info "Сборка с неверным паролем должна упасть"
run_expect_failure wrong-pass \
    env KEYSTORE_PATH="$KS" KEYSTORE_PASS="wrong-$PASS_OK" "$COPY/build.sh"

info "Сборка без пароля должна упасть"
run_expect_failure no-pass \
    env -u KEYSTORE_PASS KEYSTORE_PATH="$KS" "$COPY/build.sh"

SHA_AFTER="$(sha256sum "$APK" | awk '{print $1}')"
IDSIG_SHA_AFTER="$(sha256sum "$APK.idsig" | awk '{print $1}')"
[[ "$SHA_AFTER" == "$SHA_BEFORE" ]] || err "APK изменился после неудачных сборок"
[[ "$IDSIG_SHA_AFTER" == "$IDSIG_SHA_BEFORE" ]] || err ".idsig изменился после неудачных сборок"

info "Готово: прошлый APK сохранён (sha256 $SHA_AFTER)"
