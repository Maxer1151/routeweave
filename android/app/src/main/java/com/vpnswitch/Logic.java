package com.vpnswitch;

import java.util.ArrayList;
import java.util.BitSet;
import java.util.Collection;
import java.util.List;
import java.util.Locale;

/**
 * Чистые правила приложения без Android API.
 *
 * Здесь собраны функции, которые не зависят от контекста и системных сервисов:
 * сравнение префиксов адресов, выбор целевого VPN, проверка выполнения правила,
 * доверие Wi-Fi по паре SSID+BSSID, маскирование логов, backoff повторов и
 * проверка временного окна. Их можно покрывать обычными unit-тестами без
 * устройства и эмулятора.
 */
public final class Logic {

    private static final long MINUTE_MS = 60L * 1000L;
    private static final int RETRY_MAX_MINUTES = 30;

    private Logic() {
    }

    // --- Доверие Wi-Fi: пара SSID+BSSID ------------------------------------

    /**
     * Результат проверки доверия текущей Wi-Fi.
     *
     * NONE — проверять нечего (нет SSID/не Wi-Fi/открытая сеть);
     * UNBOUND — есть старая запись только по SSID, её нужно привязать к точке;
     * MISMATCH — SSID совпал, но BSSID другой (в том числе BSSID недоступен);
     * TRUSTED — совпали и SSID, и BSSID.
     */
    public enum Trust {
        NONE,
        UNBOUND,
        MISMATCH,
        TRUSTED
    }

    /**
     * Тип защиты текущей Wi-Fi. UNKNOWN — определить не удалось (нет данных,
     * исключение, API 29 без конфигурации): доверие не подтверждается.
     */
    public enum WifiSecurity {
        OPEN,
        SECURED,
        UNKNOWN
    }

    /**
     * Доверенная сеть. SSID нормализован, BSSID — в нижнем регистре либо пуст
     * для старой записи без привязки к точке.
     */
    public static final class TrustedEntry {
        public final String ssid;
        public final String bssid;

        public TrustedEntry(String ssid, String bssid) {
            this.ssid = normalizeSsid(ssid);
            this.bssid = this.ssid.length() == 0
                    ? "" : nullToEmpty(normalizeBssid(bssid));
        }

        public boolean isBound() {
            return bssid.length() > 0;
        }

        @Override
        public boolean equals(Object other) {
            if (this == other) {
                return true;
            }
            if (!(other instanceof TrustedEntry)) {
                return false;
            }
            TrustedEntry that = (TrustedEntry) other;
            return ssid.equals(that.ssid) && bssid.equals(that.bssid);
        }

        @Override
        public int hashCode() {
            return 31 * ssid.hashCode() + bssid.hashCode();
        }
    }

    /**
     * Нормализует SSID: убирает пробелы по краям и одну пару обрамляющих
     * двойных кавычек. Пустой или null превращается в "".
     */
    public static String normalizeSsid(String ssid) {
        if (ssid == null) {
            return "";
        }
        String value = ssid.trim();
        if (value.length() >= 2
                && value.charAt(0) == '"'
                && value.charAt(value.length() - 1) == '"') {
            value = value.substring(1, value.length() - 1).trim();
        }
        return value;
    }

    /**
     * Нормализует BSSID к виду {@code xx:xx:xx:xx:xx:xx} в нижнем регистре.
     * null возвращается для пустой строки, неверного формата, а также для
     * служебных значений {@code 00:00:00:00:00:00} и {@code 02:00:00:00:00:00},
     * которыми Android подменяет недоступный BSSID.
     */
    public static String normalizeBssid(String bssid) {
        if (bssid == null) {
            return null;
        }
        String value = bssid.trim().toLowerCase(Locale.US);
        if (value.length() != 17) {
            return null;
        }
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            if (i % 3 == 2) {
                if (c != ':') {
                    return null;
                }
            } else if (hexValue(c) < 0) {
                return null;
            }
        }
        if ("00:00:00:00:00:00".equals(value)
                || "02:00:00:00:00:00".equals(value)) {
            return null;
        }
        return value;
    }

    /** Запись для хранения в trusted_ssids: {@code ssid + "\t" + bssid}. */
    public static String formatTrustedEntry(String ssid, String bssid) {
        String normalizedSsid = normalizeSsid(ssid);
        if (normalizedSsid.length() == 0) {
            return "";
        }
        String normalizedBssid = normalizeBssid(bssid);
        if (normalizedBssid == null) {
            return normalizedSsid;
        }
        return normalizedSsid + "\t" + normalizedBssid;
    }

    /** Запись для UI: {@code SSID | BSSID}; без BSSID — только SSID. */
    public static String displayTrustedEntry(String ssid, String bssid) {
        String normalizedSsid = normalizeSsid(ssid);
        if (normalizedSsid.length() == 0) {
            return "";
        }
        String normalizedBssid = normalizeBssid(bssid);
        if (normalizedBssid == null) {
            return normalizedSsid;
        }
        return normalizedSsid + " | " + normalizedBssid;
    }

    /**
     * Разбирает запись из prefs или поля ввода. Поддерживается формат с табом
     * (хранение) и {@code " | "} (UI). Невалидный BSSID даёт запись без
     * привязки к точке; пустой SSID — null.
     */
    public static TrustedEntry parseTrustedEntry(String raw) {
        if (raw == null) {
            return null;
        }
        String value = raw.trim();
        if (value.length() == 0) {
            return null;
        }
        int separator = value.lastIndexOf('\t');
        int separatorLength = 1;
        if (separator < 0) {
            separator = value.lastIndexOf(" | ");
            separatorLength = 3;
        }
        String ssidPart = separator < 0 ? value : value.substring(0, separator);
        String bssidPart = separator < 0
                ? "" : value.substring(separator + separatorLength);
        String ssid = normalizeSsid(ssidPart);
        if (ssid.length() == 0) {
            return null;
        }
        return new TrustedEntry(ssid, bssidPart);
    }

    /**
     * Разбирает список доверенных сетей: ровно одна запись в строке. Запятая и
     * точка с запятой — допустимые символы SSID, поэтому разделителями не
     * считаются. Пустые строки пропускаются.
     */
    public static List<TrustedEntry> parseTrustedEntries(String raw) {
        List<TrustedEntry> result = new ArrayList<>();
        if (raw == null) {
            return result;
        }
        for (String line : raw.split("\\r?\\n|\\r")) {
            TrustedEntry entry = parseTrustedEntry(line);
            if (entry != null) {
                result.add(entry);
            }
        }
        return result;
    }

    // Значения android.net.wifi.WifiInfo SECURITY_TYPE_* и
    // android.net.wifi.WifiConfiguration.KeyMgmt.* продублированы, чтобы
    // классификация проверялась обычными unit-тестами без Android SDK.
    public static final int WIFI_SECURITY_TYPE_OPEN = 0;
    public static final int WIFI_SECURITY_TYPE_OWE = 6;
    public static final int WIFI_SECURITY_TYPE_OSEN = 10;
    private static final int KEY_MGMT_NONE = 0;
    private static final int KEY_MGMT_OSEN = 5;
    private static final int KEY_MGMT_OWE = 9;

    /**
     * Тип защиты из WifiInfo.getCurrentSecurityType() (API 30+). OWE и OSEN —
     * сети без учётных данных, поэтому для доверия они не отличаются от
     * неизвестного типа. Неизвестные будущие значения тоже UNKNOWN: список
     * защищённых типов перечислен явно, чтобы не доверять сети по ошибке.
     */
    public static WifiSecurity wifiSecurityFromType(int type) {
        if (type == WIFI_SECURITY_TYPE_OPEN) {
            return WifiSecurity.OPEN;
        }
        switch (type) {
            // WEP, PSK, EAP, SAE, 192-bit, WAPI, WPA3-Enterprise, Passpoint, DPP.
            case 1:
            case 2:
            case 3:
            case 4:
            case 5:
            case 7:
            case 8:
            case 9:
            case 11:
            case 12:
            case 13:
                return WifiSecurity.SECURED;
            default:
                // OWE (6), OSEN (10), UNKNOWN (-1) и всё незнакомое.
                return WifiSecurity.UNKNOWN;
        }
    }

    /**
     * Тип защиты из WifiConfiguration.allowedKeyManagement (API 29). Пустой или
     * null-набор означает «тип не определён», а не «открытая сеть». OWE/OSEN
     * также не подтверждают доверие.
     */
    public static WifiSecurity wifiSecurityFromKeyManagement(BitSet allowed) {
        if (allowed == null || allowed.isEmpty()) {
            return WifiSecurity.UNKNOWN;
        }
        if (allowed.length() > 18 || allowed.get(KEY_MGMT_OWE) || allowed.get(KEY_MGMT_OSEN)) {
            // Незнакомый бит или сеть без учётных данных: не угадываем.
            return WifiSecurity.UNKNOWN;
        }
        boolean onlyNone = allowed.cardinality() == 1 && allowed.get(KEY_MGMT_NONE);
        return onlyNone ? WifiSecurity.OPEN : WifiSecurity.SECURED;
    }

    /**
     * Уровень доверия текущей сети. Fail-closed: открытая сеть, неизвестный тип
     * защиты, пустой SSID и недоступный BSSID не дают TRUSTED. Несколько точек
     * одного SSID поддерживаются как несколько записей (mesh/роуминг).
     */
    public static Trust trustLevel(Collection<TrustedEntry> entries, String ssid,
                                   String bssid, WifiSecurity security) {
        String normalizedSsid = normalizeSsid(ssid);
        if (normalizedSsid.length() == 0 || security != WifiSecurity.SECURED) {
            return Trust.NONE;
        }
        if (entries == null || entries.isEmpty()) {
            return Trust.NONE;
        }
        String normalizedBssid = normalizeBssid(bssid);
        boolean ssidMatched = false;
        boolean unbound = false;
        boolean matched = false;
        for (TrustedEntry entry : entries) {
            if (entry == null || entry.ssid.length() == 0
                    || !entry.ssid.equals(normalizedSsid)) {
                continue;
            }
            ssidMatched = true;
            if (entry.bssid.length() == 0) {
                unbound = true;
            } else if (normalizedBssid != null && entry.bssid.equals(normalizedBssid)) {
                matched = true;
            }
        }
        if (!ssidMatched) {
            return Trust.NONE;
        }
        if (matched) {
            return Trust.TRUSTED;
        }
        if (unbound) {
            return Trust.UNBOUND;
        }
        return Trust.MISMATCH;
    }

    // --- Ручное снятие exit node -------------------------------------------

    /**
     * Ключ сети для запоминания ручного снятия exit node. SSID нормализуется,
     * BSSID намеренно не входит: роуминг между точками одного SSID не должен
     * снимать ручной запрет. Третий пустой сегмент сохраняет совместимость с
     * прежним форматом {@code WIFI|SSID|BSSID}, где BSSID теперь всегда пуст.
     */
    public static String networkKey(NetKind kind, String ssid) {
        String kindName = (kind == null ? NetKind.NONE : kind).name();
        return kindName + "|" + normalizeSsid(ssid) + "|";
    }

    /**
     * true, если долг снятия относится уже к другой сети. Смена BSSID у того
     * же SSID — тоже смена сети: долг снятия привязан к точке, а не только к
     * имени сети.
     */
    public static boolean offNetworkChanged(boolean ssidChanged, boolean bssidChanged) {
        return ssidChanged || bssidChanged;
    }

    /**
     * true, если сохранённый и текущий BSSID известны и различаются. Пустой
     * BSSID смену не доказывает: на API 29 и без разрешений он может быть
     * недоступен, и тогда сеть считается прежней.
     */
    public static boolean offBssidChanged(String savedBssid, String currentBssid) {
        String saved = normalizeBssid(savedBssid);
        String current = normalizeBssid(currentBssid);
        return saved != null && current != null && !saved.equals(current);
    }

    /**
     * true, если сохранённый ручной запрет всё ещё относится к текущей сети.
     * Сравниваются тип сети и SSID; старые ключи с BSSID читаются так же.
     * Пустой сохранённый SSID считается «сеть не опознана» и сохраняет запрет,
     * пока SSID не появится; пустой текущий SSID смену сети не доказывает.
     */
    public static boolean manualOffApplies(String savedKey, String currentKey) {
        if (savedKey == null || savedKey.length() == 0) {
            return false;
        }
        if (currentKey == null || currentKey.length() == 0) {
            return true;
        }
        String savedKind = manualOffKind(savedKey);
        String currentKind = manualOffKind(currentKey);
        if (savedKind.length() == 0 || !savedKind.equals(currentKind)) {
            // Другой тип сети: Wi-Fi -> мобильная или наоборот.
            return false;
        }
        String savedSsid = manualOffSsid(savedKey);
        String currentSsid = manualOffSsid(currentKey);
        if (savedSsid.length() == 0) {
            // Запрет поставлен, когда SSID был недоступен: при появлении SSID
            // считаем сеть той же и мигрируем ключ.
            return true;
        }
        if (currentSsid.length() == 0) {
            // Смену сети доказать нельзя — запрет сохраняется.
            return true;
        }
        return savedSsid.equals(currentSsid);
    }

    /**
     * Канонический ключ после проверки: мигрирует старые ключи с BSSID и
     * {@code WIFI||} к виду {@code WIFI|SSID|}. Пустой результат — запрет
     * больше не относится к текущей сети.
     */
    public static String manualOffKeyAfterCheck(String savedKey, String currentKey) {
        if (!manualOffApplies(savedKey, currentKey)) {
            return "";
        }
        String kind = manualOffKind(currentKey);
        if (kind.length() == 0) {
            kind = manualOffKind(savedKey);
        }
        String ssid = manualOffSsid(currentKey);
        if (ssid.length() == 0) {
            ssid = manualOffSsid(savedKey);
        }
        NetKind parsedKind;
        try {
            parsedKind = kind.length() == 0 ? NetKind.NONE : NetKind.valueOf(kind);
        } catch (IllegalArgumentException e) {
            parsedKind = NetKind.NONE;
        }
        return networkKey(parsedKind, ssid);
    }

    /** Удобная форма для UI: сравнить сохранённый ключ с текущей сетью. */
    public static boolean manualOffApplies(String savedKey, NetKind kind, String ssid) {
        return manualOffApplies(savedKey, networkKey(kind, ssid));
    }

    private static String manualOffKind(String key) {
        if (key == null) {
            return "";
        }
        int bar = key.indexOf('|');
        return bar < 0 ? "" : key.substring(0, bar);
    }

    /**
     * SSID из ключа. Новый формат всегда заканчивается пустым сегментом BSSID;
     * старый — может содержать MAC или пустой хвост. Один хвостовой сегмент
     * снимается всегда, MAC — только если он действительно похож на BSSID.
     */
    private static String manualOffSsid(String key) {
        if (key == null) {
            return "";
        }
        int bar = key.indexOf('|');
        if (bar < 0) {
            return "";
        }
        String rest = key.substring(bar + 1);
        if (rest.endsWith("|")) {
            return rest.substring(0, rest.length() - 1);
        }
        int last = rest.lastIndexOf('|');
        if (last >= 0 && normalizeBssid(rest.substring(last + 1)) != null) {
            return rest.substring(0, last);
        }
        return rest;
    }

    /** Верхний предел попыток снятия: после него долг переходит в BLOCKED. */
    public static final int MAX_EXIT_OFF_ATTEMPTS = 5;

    /**
     * Шаг снятия exit node. Вызывается только при активном «долге» снятия:
     *
     * DEFER — Tailscale не подключён, команду отправить нельзя;
     * START — начать стартовое окно (deadline ещё не задан): Tailscale получает
     *         время восстановить сохранённый exit node;
     * SEND — отправить команду снятия (в том числе повтор);
     * WAIT — идёт окно, подтверждение ещё не наступило;
     * CONFIRMED — exit node фактически не активен после окна;
     * FAILED — окно истекло, exit node всё ещё активен, попытки остались;
     * BLOCKED — попытки исчерпаны, автоматические повторы прекращены.
     *
     * Окно нужно, чтобы Tailscale успел восстановить exit node после
     * переподключения: «не активен» сразу после connect не считается снятием.
     */
    public enum ExitOffStep {
        DEFER,
        START,
        SEND,
        WAIT,
        CONFIRMED,
        FAILED,
        BLOCKED
    }

    public static ExitOffStep exitOffStep(boolean tailscaleActive, boolean exitActive,
                                          boolean sent, long deadline, long now) {
        return exitOffStep(tailscaleActive, exitActive, sent, deadline, now, 0, false);
    }

    /**
     * Расширенный шаг снятия с учётом исчерпания попыток. blocked сохраняется
     * между запусками: после BLOCKED автоматические SEND/FAILED прекращаются,
     * но фактическое исчезновение exit node всё ещё подтверждается.
     */
    public static ExitOffStep exitOffStep(boolean tailscaleActive, boolean exitActive,
                                          boolean sent, long deadline, long now,
                                          int failedAttempts, boolean blocked) {
        if (!tailscaleActive) {
            return ExitOffStep.DEFER;
        }
        boolean windowOver = deadline > 0L && now >= deadline;
        if (blocked) {
            // После BLOCKED команды не отправляются, но исчезновение ноды
            // всё ещё подтверждается — в том числе если её сняли вручную.
            return exitActive ? ExitOffStep.BLOCKED : ExitOffStep.CONFIRMED;
        }
        if (deadline <= 0L) {
            return ExitOffStep.START;
        }
        if (!sent) {
            if (!windowOver) {
                return ExitOffStep.WAIT;
            }
            // Повтор после неудачной отправки: попытки тоже ограничены.
            return failedAttempts >= MAX_EXIT_OFF_ATTEMPTS
                    ? ExitOffStep.BLOCKED : ExitOffStep.SEND;
        }
        if (!exitActive) {
            return windowOver ? ExitOffStep.CONFIRMED : ExitOffStep.WAIT;
        }
        if (!windowOver) {
            return ExitOffStep.WAIT;
        }
        return failedAttempts >= MAX_EXIT_OFF_ATTEMPTS
                ? ExitOffStep.BLOCKED : ExitOffStep.FAILED;
    }

    // --- Цель exit node и долг снятия --------------------------------------

    /** Что делать с активным долгом снятия при обнаружении смены сети. */
    public enum OffDebtAction {
        /** Сеть та же или смену доказать нельзя: состояние не меняется. */
        KEEP,
        /** Сеть требует exit node: долг снятия отменяется. */
        CANCEL,
        /** Сеть не требует exit node: попытки снятия перезапускаются. */
        RESTART
    }

    /**
     * Причина, по которой взведён долг снятия. Нужна, чтобы отличить
     * watchdog от правила и ручных действий: watchdog на той же сети
     * отменять нельзя, иначе он включит ноду и снова снимет её из-за
     * отсутствия интернета.
     */
    public enum OffDebtSource {
        /** Снятие из-за отсутствия интернета через exit node. */
        WATCHDOG,
        /** Снятие по правилу: доверенная Wi-Fi или мобильная сеть. */
        RULE,
        /** Ручное «Снять сейчас». */
        MANUAL,
        /** Имя exit node очищено. */
        NAME,
        /** Управление exit node выключено (долг при этом очищается сразу). */
        DISABLE,
        /** Долг старой версии без сохранённого источника. */
        UNKNOWN;

        /** true, если долг нельзя отменять на той же сети. */
        public boolean keepsDebtOnSameNetwork() {
            return this == WATCHDOG;
        }
    }

    /** Разбирает сохранённый источник долга; неизвестное значение — UNKNOWN. */
    public static OffDebtSource parseOffDebtSource(String raw) {
        if (raw == null || raw.trim().length() == 0) {
            return OffDebtSource.UNKNOWN;
        }
        try {
            return OffDebtSource.valueOf(raw.trim());
        } catch (IllegalArgumentException e) {
            return OffDebtSource.UNKNOWN;
        }
    }

    /** Источник запуска exit node: полный tick или сетевое событие. */
    public enum TickSource {
        /** Полный tick: сервис, onStartCommand, подтверждение, ручное подключение. */
        PERIODIC,
        /** Сетевое событие: только правило и пересчёт долга, без проб. */
        NETWORK
    }

    /**
     * Watchdog и retry выполняются только в штатном периодическом tick.
     * Сетевые callback'и приходят пачками (onAvailable/onLost/...), и пробы
     * интернета в них давали бы ложные снятия exit node.
     */
    public static boolean runsBackgroundChecks(TickSource source) {
        return source == TickSource.PERIODIC;
    }

    /**
     * Правило Wi-Fi требует включённый exit node: управление включено, имя
     * задано, сеть незнакомая. Доверенная Wi-Fi, мобильная сеть и пустое имя
     * ноду не требуют.
     */
    public static boolean ruleRequiresExitNode(boolean manageEnabled, boolean hasName,
                                               NetKind kind, boolean trusted) {
        return manageEnabled && hasName && kind == NetKind.WIFI && !trusted;
    }

    /**
     * Итоговая цель с учётом ручного снятия: пользовательский запрет важнее
     * правила. Пока запрет действует, exit node включать нельзя.
     */
    public static boolean exitNodeWanted(boolean ruleWants, boolean manualOff) {
        return ruleWants && !manualOff;
    }

    /**
     * Решение по долгу снятия при смене сети. Если новая сеть требует exit
     * node, команду off отправлять нельзя: долг отменяется, и правило снова
     * включает нужную ноду. Иначе попытки снятия перезапускаются.
     */
    public static OffDebtAction offDebtActionOnNetworkChange(boolean networkChanged,
                                                             boolean exitNodeWanted) {
        if (!networkChanged) {
            return OffDebtAction.KEEP;
        }
        return exitNodeWanted ? OffDebtAction.CANCEL : OffDebtAction.RESTART;
    }

    /**
     * Долг снятия на той же сети. Если правило снова требует exit node
     * (например, сеть перестала быть доверенной или имя ноды вернули),
     * долг отменяется и правило может включить ноду. Исключение — watchdog:
     * его отмена на той же сети дала бы цикл «включить → снять из-за
     * отсутствия интернета».
     */
    public static OffDebtAction offDebtActionOnSameNetwork(boolean exitNodeWanted,
                                                           OffDebtSource source) {
        OffDebtSource actual = source == null ? OffDebtSource.UNKNOWN : source;
        if (exitNodeWanted && !actual.keepsDebtOnSameNetwork()) {
            return OffDebtAction.CANCEL;
        }
        return OffDebtAction.KEEP;
    }

    /**
     * true, если активную попытку снятия нужно приостановить: Tailscale не
     * подключён и правило больше не требует exit node. Владение при этом не
     * очищается — оно хранится до фактического подтверждения.
     */
    public static boolean shouldDeferExitOff(boolean tailscaleActive, boolean exitNodeWanted) {
        return !tailscaleActive && !exitNodeWanted;
    }

    /** true, если приложению всё ещё принадлежит включённый exit node. */
    public static boolean hasExitOffOwnership(boolean applied, boolean appliedNamePresent) {
        return applied || appliedNamePresent;
    }

    /**
     * true, если отложенное снятие пора возобновить: Tailscale подключён,
     * правило не требует exit node, а владение сохранено.
     */
    public static boolean shouldResumeOwnedOff(boolean tailscaleActive, boolean exitNodeWanted,
                                               boolean ownership) {
        return tailscaleActive && !exitNodeWanted && ownership;
    }

    /**
     * Адресная очистка уведомления об ошибке: requested == null — очистить
     * любую; пустой current — ошибка старого формата без источника.
     */
    public static boolean errorSourceMatches(String current, String requested) {
        return requested == null || current == null || current.length() == 0
                || requested.equals(current);
    }

    /**
     * Маскирует хост для логов: IPv4 — первый октет, IPv6 — первая группа,
     * имя — первый символ. Используется только для диагностики, не для UI.
     */
    public static String maskHost(String host) {
        if (host == null) {
            return "<empty>";
        }
        String value = host.trim();
        if (value.length() == 0) {
            return "<empty>";
        }
        if (isIpv4(value)) {
            return value.substring(0, value.indexOf('.')) + ".***.***.***";
        }
        int colon = value.indexOf(':');
        if (colon >= 0) {
            String first = value.substring(0, colon);
            return first.length() == 0 ? ":…" : first + ":…";
        }
        return value.charAt(0) + "***";
    }

    private static boolean isIpv4(String value) {
        int dots = 0;
        int partStart = 0;
        for (int i = 0; i <= value.length(); i++) {
            char c = i < value.length() ? value.charAt(i) : '.';
            if (c == '.') {
                int length = i - partStart;
                if (length < 1 || length > 3) {
                    return false;
                }
                int number = 0;
                for (int j = partStart; j < i; j++) {
                    char digit = value.charAt(j);
                    if (digit < '0' || digit > '9') {
                        return false;
                    }
                    number = number * 10 + (digit - '0');
                }
                if (number > 255) {
                    return false;
                }
                dots++;
                partStart = i + 1;
                if (dots > 4) {
                    return false;
                }
            }
        }
        return dots == 4;
    }

    private static int hexValue(char c) {
        if (c >= '0' && c <= '9') {
            return c - '0';
        }
        if (c >= 'a' && c <= 'f') {
            return c - 'a' + 10;
        }
        return -1;
    }

    private static String nullToEmpty(String value) {
        return value == null ? "" : value;
    }

    /**
     * true, если первые {@code prefixLength} бит адреса совпадают с сетью.
     * Адреса должны быть одной длины (IPv4 или IPv6).
     */
    public static boolean prefixMatches(byte[] network, int prefixLength, byte[] address) {
        if (network == null || address == null || network.length != address.length) {
            return false;
        }
        if (prefixLength < 0 || prefixLength > network.length * 8) {
            return false;
        }
        int fullBytes = prefixLength / 8;
        for (int i = 0; i < fullBytes; i++) {
            if (network[i] != address[i]) {
                return false;
            }
        }
        int remainder = prefixLength % 8;
        if (remainder != 0) {
            int mask = (0xFF << (8 - remainder)) & 0xFF;
            if ((network[fullBytes] & mask) != (address[fullBytes] & mask)) {
                return false;
            }
        }
        return true;
    }

    /**
     * true, если маршрут целиком лежит внутри целевого префикса:
     * префикс маршрута не короче целевого и адрес совпадает по целевым битам.
     */
    public static boolean routeWithin(byte[] routeAddr, int routePrefix,
                                      byte[] targetAddr, int targetPrefix) {
        if (routeAddr == null || targetAddr == null) {
            return false;
        }
        if (routePrefix < 0 || routePrefix > routeAddr.length * 8) {
            return false;
        }
        if (routePrefix < targetPrefix) {
            return false;
        }
        return prefixMatches(targetAddr, targetPrefix, routeAddr);
    }

    /** Целевой VPN по текущей сети: доверенная Wi-Fi — none, незнакомая — Tailscale, моб. — WDTT. */
    public static VpnType targetFor(NetKind kind, boolean trustedWifi) {
        if (kind == null) {
            return VpnType.NONE;
        }
        switch (kind) {
            case WIFI:
                return trustedWifi ? VpnType.NONE : VpnType.TAILSCALE;
            case MOBILE:
                return VpnType.WDTT;
            case OTHER:
            case NONE:
            default:
                return VpnType.NONE;
        }
    }

    /**
     * Выполнено ли правило для текущей сети и активного VPN.
     * Для незнакомой Wi-Fi при включённом управлении exit node одного Tailscale
     * недостаточно: нужен реально активный exit node (pending/confirm не в счёт).
     */
    public static boolean ruleSatisfied(NetKind kind, boolean vpnActive, VpnType type,
                                        boolean trusted, boolean exitRequired,
                                        boolean exitActive) {
        if (kind == null) {
            return false;
        }
        VpnType actual = type == null ? VpnType.NONE : type;
        switch (kind) {
            case WIFI:
                if (trusted) {
                    return !vpnActive;
                }
                return vpnActive && actual == VpnType.TAILSCALE
                        && (!exitRequired || exitActive);
            case MOBILE:
                return vpnActive && actual == VpnType.WDTT;
            case OTHER:
            case NONE:
            default:
                return false;
        }
    }

    /**
     * Задержка перед следующей попыткой восстановления exit node:
     * 5, 10, 20, 30 минут, дальше — не больше 30.
     */
    public static int retryDelayMs(int attempt) {
        int index = Math.max(1, attempt);
        long minutes;
        if (index == 1) {
            minutes = 5L;
        } else if (index == 2) {
            minutes = 10L;
        } else if (index == 3) {
            minutes = 20L;
        } else {
            minutes = RETRY_MAX_MINUTES;
        }
        return (int) (minutes * MINUTE_MS);
    }

    /** true, если момент {@code at} был не позже {@code now} и не старше окна. */
    public static boolean withinWindow(long now, long at, long windowMs) {
        if (at <= 0L || windowMs < 0L) {
            return false;
        }
        long diff = now - at;
        return diff >= 0L && diff <= windowMs;
    }
}
