#!/usr/bin/env bash
# Unit-тесты чистой логики приложения: без Android SDK, Gradle и JUnit.
#
# Компилирует Logic.java, NetKind.java, VpnType.java и LogicTest.java во
# временный каталог и запускает com.vpnswitch.LogicTest. В репозитории ничего
# не остаётся: каталог удаляется по trap.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "$SCRIPT_DIR/../app/src/main/java/com/vpnswitch" && pwd)"
TEST_SOURCE="$SCRIPT_DIR/LogicTest.java"

info() {
    echo "==> $*"
}

err() {
    echo "ОШИБКА: $*" >&2
    exit 1
}

# JDK: JAVA_HOME приоритетнее PATH, как в build.sh.
if [[ -n "${JAVA_HOME:-}" && -x "$JAVA_HOME/bin/javac" ]]; then
    JAVAC="$JAVA_HOME/bin/javac"
    JAVA="$JAVA_HOME/bin/java"
elif command -v javac >/dev/null 2>&1; then
    JAVAC="$(command -v javac)"
    # java ищем рядом с javac: в PATH может оказаться javac без java.
    JAVAC_REAL="$(readlink -f "$JAVAC")"
    JAVA_HOME_REAL="$(dirname "$(dirname "$JAVAC_REAL")")"
    if [[ -x "$JAVA_HOME_REAL/bin/java" ]]; then
        JAVA="$JAVA_HOME_REAL/bin/java"
    else
        JAVA="$(command -v java || true)"
    fi
else
    err "JDK не найден. Укажите JAVA_HOME или добавьте javac в PATH"
fi

[[ -n "$JAVA" && -x "$JAVA" ]] || err "не найден java (JAVA_HOME=${JAVA_HOME:-не задан})"

for name in Logic.java NetKind.java VpnType.java; do
    [[ -f "$SRC_DIR/$name" ]] || err "нет исходника $SRC_DIR/$name"
done
[[ -f "$TEST_SOURCE" ]] || err "нет теста $TEST_SOURCE"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/routeweave-logic-tests.XXXXXX")"
cleanup() {
    rm -rf "$WORK"
}
trap cleanup EXIT HUP INT TERM

CLASSES="$WORK/classes"
mkdir -p "$CLASSES"

info "javac: $JAVAC"
info "Исходники: $SRC_DIR"
# -encoding UTF-8: в комментариях и сообщениях тестов есть кириллица, а
# локаль runner'а может быть C.
"$JAVAC" -encoding UTF-8 -d "$CLASSES" \
    "$SRC_DIR/Logic.java" \
    "$SRC_DIR/NetKind.java" \
    "$SRC_DIR/VpnType.java" \
    "$TEST_SOURCE"

info "Запуск com.vpnswitch.LogicTest"
"$JAVA" -cp "$CLASSES" com.vpnswitch.LogicTest

info "Готово"
