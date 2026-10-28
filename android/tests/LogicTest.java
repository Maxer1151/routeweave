package com.vpnswitch;

import com.vpnswitch.Logic.TrustedEntry;

import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.BitSet;
import java.util.Collections;
import java.util.List;

/**
 * Unit-тесты чистой логики {@link Logic} без JUnit, Gradle и Android SDK.
 *
 * Запускается обычной JVM: {@code java -cp <classes> com.vpnswitch.LogicTest}.
 * Проверки собраны по фактическому поведению Logic.java; при расхождении
 * печатается сообщение и код возврата равен 1, при успехе — "ALL OK".
 */
public final class LogicTest {

    private static final int MINUTE_MS = 60 * 1000;
    private static final int RETRY_CAP_MS = 30 * MINUTE_MS;

    private static int checks;
    private static int failures;

    private LogicTest() {
    }

    public static void main(String[] args) {
        // Русские сообщения должны читаться и на runner'ах с C-локалью.
        System.setOut(new PrintStream(new FileOutputStream(FileDescriptor.out), true,
                StandardCharsets.UTF_8));

        ssidNormalization();
        bssidNormalization();
        trustedFormatting();
        trustedParsing();
        trustedEquality();
        trustLevels();
        securityTypes();
        trustedLists();
        networkKeys();
        exitOffSteps();
        exitOffGoals();
        networkTickPolicy();
        errorSources();
        hostMasking();
        prefixes();
        routes();
        targets();
        rules();
        retryDelays();
        windows();

        if (failures > 0) {
            System.out.println("ПРОВАЛЕНО: " + failures + " из " + checks);
            System.exit(1);
        }
        System.out.println("Проверок: " + checks);
        System.out.println("ALL OK");
    }

    // --- normalizeSsid ------------------------------------------------------

    private static void ssidNormalization() {
        eq("normalizeSsid(null)", "", Logic.normalizeSsid(null));
        eq("normalizeSsid(пустая строка)", "", Logic.normalizeSsid(""));
        eq("normalizeSsid(только пробелы)", "", Logic.normalizeSsid("   \t "));
        eq("normalizeSsid(trim)", "Home", Logic.normalizeSsid("  Home  "));
        eq("normalizeSsid(обрамляющие кавычки)", "Home", Logic.normalizeSsid("\"Home\""));
        eq("normalizeSsid(кавычки и пробелы)", "Home", Logic.normalizeSsid("  \" Home \"  "));
        eq("normalizeSsid(пустые кавычки)", "", Logic.normalizeSsid("\"\""));
        eq("normalizeSsid(снимается только одна пара кавычек)",
                "\"Home\"", Logic.normalizeSsid("\"\"Home\"\""));
        eq("normalizeSsid(непарная кавычка в начале)", "\"Home", Logic.normalizeSsid("\"Home"));
        eq("normalizeSsid(непарная кавычка в конце)", "Home\"", Logic.normalizeSsid("Home\""));
        eq("normalizeSsid(один символ кавычки)", "\"", Logic.normalizeSsid("\""));
        eq("normalizeSsid(пробелы внутри сохраняются)", "My Home", Logic.normalizeSsid(" My Home "));
    }

    // --- normalizeBssid -----------------------------------------------------

    private static void bssidNormalization() {
        eq("normalizeBssid(null)", null, Logic.normalizeBssid(null));
        eq("normalizeBssid(пусто)", null, Logic.normalizeBssid(""));
        eq("normalizeBssid(пробелы)", null, Logic.normalizeBssid("   "));
        eq("normalizeBssid(нижний регистр)",
                "aa:bb:cc:dd:ee:ff", Logic.normalizeBssid("aa:bb:cc:dd:ee:ff"));
        eq("normalizeBssid(верхний регистр)",
                "aa:bb:cc:dd:ee:ff", Logic.normalizeBssid("AA:BB:CC:DD:EE:FF"));
        eq("normalizeBssid(смешанный регистр и пробелы)",
                "aa:bb:cc:dd:ee:ff", Logic.normalizeBssid("  Aa:Bb:Cc:Dd:Ee:Ff  "));
        eq("normalizeBssid(дефисы вместо двоеточий)",
                null, Logic.normalizeBssid("aa-bb-cc-dd-ee-ff"));
        eq("normalizeBssid(без разделителей)", null, Logic.normalizeBssid("aabbccddeeff"));
        eq("normalizeBssid(лишняя группа)", null, Logic.normalizeBssid("aa:bb:cc:dd:ee:ff:00"));
        eq("normalizeBssid(недостающая группа)", null, Logic.normalizeBssid("aa:bb:cc:dd:ee"));
        eq("normalizeBssid(не hex)", null, Logic.normalizeBssid("gg:bb:cc:dd:ee:ff"));
        eq("normalizeBssid(не хватает символа)", null, Logic.normalizeBssid("aa:bb:cc:dd:ee:f"));
        eq("normalizeBssid(служебный 00:00:00:00:00:00)",
                null, Logic.normalizeBssid("00:00:00:00:00:00"));
        eq("normalizeBssid(служебный 02:00:00:00:00:00)",
                null, Logic.normalizeBssid("02:00:00:00:00:00"));
    }

    // --- formatTrustedEntry / displayTrustedEntry ---------------------------

    private static void trustedFormatting() {
        eq("formatTrustedEntry(с BSSID)",
                "Home\taa:bb:cc:dd:ee:ff",
                Logic.formatTrustedEntry(" Home ", "AA:BB:CC:DD:EE:FF"));
        eq("formatTrustedEntry(кавычки в SSID)",
                "Home\taa:bb:cc:dd:ee:ff",
                Logic.formatTrustedEntry("\"Home\"", "aa:bb:cc:dd:ee:ff"));
        eq("formatTrustedEntry(без BSSID)", "Home", Logic.formatTrustedEntry("Home", null));
        eq("formatTrustedEntry(битый BSSID)", "Home", Logic.formatTrustedEntry("Home", "bad"));
        eq("formatTrustedEntry(служебный BSSID)",
                "Home", Logic.formatTrustedEntry("Home", "00:00:00:00:00:00"));
        eq("formatTrustedEntry(пустой SSID)",
                "", Logic.formatTrustedEntry("  ", "aa:bb:cc:dd:ee:ff"));
        eq("formatTrustedEntry(null SSID)",
                "", Logic.formatTrustedEntry(null, "aa:bb:cc:dd:ee:ff"));

        eq("displayTrustedEntry(с BSSID)",
                "Home | aa:bb:cc:dd:ee:ff",
                Logic.displayTrustedEntry(" Home ", "AA:BB:CC:DD:EE:FF"));
        eq("displayTrustedEntry(без BSSID)", "Home", Logic.displayTrustedEntry("Home", null));
        eq("displayTrustedEntry(битый BSSID)", "Home", Logic.displayTrustedEntry("Home", "nope"));
        eq("displayTrustedEntry(пустой SSID)",
                "", Logic.displayTrustedEntry("", "aa:bb:cc:dd:ee:ff"));
        eq("displayTrustedEntry(только пробелы)",
                "", Logic.displayTrustedEntry("   ", null));
    }

    // --- parseTrustedEntry --------------------------------------------------

    private static void trustedParsing() {
        eq("parseTrustedEntry(null)", null, Logic.parseTrustedEntry(null));
        eq("parseTrustedEntry(пусто)", null, Logic.parseTrustedEntry("   "));

        TrustedEntry legacy = Logic.parseTrustedEntry(" Home ");
        check("parseTrustedEntry(legacy) не null", legacy != null);
        if (legacy != null) {
            eq("parseTrustedEntry(legacy) ssid", "Home", legacy.ssid);
            eq("parseTrustedEntry(legacy) bssid", "", legacy.bssid);
            check("parseTrustedEntry(legacy) isBound false", !legacy.isBound());
        }

        TrustedEntry stored = Logic.parseTrustedEntry(" Home\tAA:BB:CC:DD:EE:FF ");
        check("parseTrustedEntry(таб) не null", stored != null);
        if (stored != null) {
            eq("parseTrustedEntry(таб) ssid", "Home", stored.ssid);
            eq("parseTrustedEntry(таб) bssid", "aa:bb:cc:dd:ee:ff", stored.bssid);
            check("parseTrustedEntry(таб) isBound", stored.isBound());
        }

        TrustedEntry ui = Logic.parseTrustedEntry(" Home | AA:BB:CC:DD:EE:FF ");
        check("parseTrustedEntry(\" | \") не null", ui != null);
        if (ui != null) {
            eq("parseTrustedEntry(\" | \") ssid", "Home", ui.ssid);
            eq("parseTrustedEntry(\" | \") bssid", "aa:bb:cc:dd:ee:ff", ui.bssid);
            check("parseTrustedEntry(\" | \") isBound", ui.isBound());
        }

        eq("parseTrustedEntry(пустой SSID в кавычках перед табом)",
                null, Logic.parseTrustedEntry("\"\"\taa:bb:cc:dd:ee:ff"));
        eq("parseTrustedEntry(пустой SSID в кавычках перед \" | \")",
                null, Logic.parseTrustedEntry("\"\" | aa:bb:cc:dd:ee:ff"));

        TrustedEntry brokenStored = Logic.parseTrustedEntry("Home\tnot-a-bssid");
        check("parseTrustedEntry(битый BSSID) не null", brokenStored != null);
        if (brokenStored != null) {
            eq("parseTrustedEntry(битый BSSID) ssid", "Home", brokenStored.ssid);
            eq("parseTrustedEntry(битый BSSID) bssid", "", brokenStored.bssid);
            check("parseTrustedEntry(битый BSSID) isBound false", !brokenStored.isBound());
        }

        TrustedEntry brokenUi = Logic.parseTrustedEntry("Home | not-a-bssid");
        check("parseTrustedEntry(битый BSSID в UI) не null", brokenUi != null);
        if (brokenUi != null) {
            eq("parseTrustedEntry(битый BSSID в UI) bssid", "", brokenUi.bssid);
            check("parseTrustedEntry(битый BSSID в UI) isBound false", !brokenUi.isBound());
        }

        TrustedEntry original = new TrustedEntry(" Home ", "AA:BB:CC:DD:EE:FF");
        check("round-trip: хранение",
                original.equals(Logic.parseTrustedEntry(
                        Logic.formatTrustedEntry(" Home ", "AA:BB:CC:DD:EE:FF"))));
        check("round-trip: UI",
                original.equals(Logic.parseTrustedEntry(
                        Logic.displayTrustedEntry(original.ssid, original.bssid))));

        TrustedEntry unbound = new TrustedEntry("Home", null);
        check("round-trip: legacy-хранение",
                unbound.equals(Logic.parseTrustedEntry(Logic.formatTrustedEntry("Home", null))));
        check("round-trip: legacy-UI",
                unbound.equals(Logic.parseTrustedEntry(Logic.displayTrustedEntry("Home", null))));
    }

    // --- TrustedEntry.equals / hashCode -------------------------------------

    private static void trustedEquality() {
        TrustedEntry a = new TrustedEntry("Home", "AA:BB:CC:DD:EE:FF");
        TrustedEntry b = new TrustedEntry(" Home ", "aa:bb:cc:dd:ee:ff");
        TrustedEntry otherAp = new TrustedEntry("Home", "aa:bb:cc:dd:ee:00");
        TrustedEntry legacy = new TrustedEntry("Home", null);
        TrustedEntry emptySsid = new TrustedEntry("", "aa:bb:cc:dd:ee:ff");
        TrustedEntry broken = new TrustedEntry("Home", "bad");

        check("equals: нормализованные записи", a.equals(b));
        check("hashCode: нормализованные записи", a.hashCode() == b.hashCode());
        check("equals: со ссылкой на себя", a.equals(a));
        check("equals: другая точка", !a.equals(otherAp));
        check("equals: привязанная и legacy", !a.equals(legacy));
        check("equals: null", !a.equals(null));
        check("equals: другой тип", !a.equals("Home\taa:bb:cc:dd:ee:ff"));

        eq("TrustedEntry(пустой SSID) ssid", "", emptySsid.ssid);
        eq("TrustedEntry(пустой SSID) bssid", "", emptySsid.bssid);
        check("TrustedEntry(пустой SSID) не привязана", !emptySsid.isBound());
        eq("TrustedEntry(битый BSSID) bssid", "", broken.bssid);
        check("TrustedEntry(битый BSSID) не привязана", !broken.isBound());
        check("isBound: привязанная", a.isBound());
        check("isBound: legacy", !legacy.isBound());
    }

    // --- trustLevel ---------------------------------------------------------

    private static void trustLevels() {
        final String ap = "aa:bb:cc:dd:ee:ff";
        final String otherAp = "aa:bb:cc:dd:ee:01";
        final Logic.WifiSecurity secured = Logic.WifiSecurity.SECURED;

        List<TrustedEntry> bound = entries(new TrustedEntry("Home", ap));
        eq("trustLevel: точное совпадение",
                Logic.Trust.TRUSTED, Logic.trustLevel(bound, "Home", ap, secured));
        eq("trustLevel: SSID и BSSID в разном регистре/пробелах",
                Logic.Trust.TRUSTED,
                Logic.trustLevel(entries(new TrustedEntry("\" Home \"", "AA:BB:CC:DD:EE:FF")),
                        " Home ", "AA:BB:CC:DD:EE:FF", secured));
        eq("trustLevel: SSID совпал, BSSID другой",
                Logic.Trust.MISMATCH, Logic.trustLevel(bound, "Home", otherAp, secured));
        eq("trustLevel: текущий BSSID служебный",
                Logic.Trust.MISMATCH,
                Logic.trustLevel(bound, "Home", "00:00:00:00:00:00", secured));
        eq("trustLevel: текущий BSSID недоступен",
                Logic.Trust.MISMATCH, Logic.trustLevel(bound, "Home", null, secured));

        List<TrustedEntry> legacy = entries(new TrustedEntry("Home", null));
        eq("trustLevel: legacy без BSSID",
                Logic.Trust.UNBOUND, Logic.trustLevel(legacy, "Home", ap, secured));
        eq("trustLevel: legacy без BSSID и текущий BSSID недоступен",
                Logic.Trust.UNBOUND, Logic.trustLevel(legacy, "Home", null, secured));

        eq("trustLevel: пустой список",
                Logic.Trust.NONE, Logic.trustLevel(entries(), "Home", ap, secured));
        eq("trustLevel: null вместо списка",
                Logic.Trust.NONE, Logic.trustLevel(null, "Home", ap, secured));
        eq("trustLevel: другой SSID",
                Logic.Trust.NONE, Logic.trustLevel(bound, "Other", ap, secured));
        eq("trustLevel: пустой SSID",
                Logic.Trust.NONE, Logic.trustLevel(bound, "", ap, secured));
        eq("trustLevel: null SSID",
                Logic.Trust.NONE, Logic.trustLevel(bound, null, ap, secured));

        // Fail-closed: только подтверждённо защищённая сеть даёт TRUSTED.
        eq("trustLevel: открытая сеть",
                Logic.Trust.NONE, Logic.trustLevel(bound, "Home", ap, Logic.WifiSecurity.OPEN));
        eq("trustLevel: неизвестный тип защиты",
                Logic.Trust.NONE, Logic.trustLevel(bound, "Home", ap, Logic.WifiSecurity.UNKNOWN));
        eq("trustLevel: null тип защиты",
                Logic.Trust.NONE, Logic.trustLevel(bound, "Home", ap, null));
        eq("trustLevel: UNKNOWN при совпадении SSID даёт NONE, а не MISMATCH",
                Logic.Trust.NONE, Logic.trustLevel(bound, "Home", otherAp,
                        Logic.WifiSecurity.UNKNOWN));
        eq("trustLevel: OPEN при legacy-записи даёт NONE, а не UNBOUND",
                Logic.Trust.NONE, Logic.trustLevel(legacy, "Home", ap,
                        Logic.WifiSecurity.OPEN));

        eq("trustLevel: несколько точек, совпала вторая",
                Logic.Trust.TRUSTED,
                Logic.trustLevel(entries(new TrustedEntry("Home", otherAp),
                        new TrustedEntry("Home", ap)), "Home", ap, secured));
        eq("trustLevel: совпавшая точка важнее legacy",
                Logic.Trust.TRUSTED,
                Logic.trustLevel(entries(new TrustedEntry("Home", null),
                        new TrustedEntry("Home", ap)), "Home", ap, secured));
        eq("trustLevel: чужая точка и legacy дают UNBOUND",
                Logic.Trust.UNBOUND,
                Logic.trustLevel(entries(new TrustedEntry("Home", otherAp),
                        new TrustedEntry("Home", null)), "Home", ap, secured));
        eq("trustLevel: null в списке пропускается",
                Logic.Trust.TRUSTED,
                Logic.trustLevel(entries((TrustedEntry) null, new TrustedEntry("Home", ap)),
                        "Home", ap, secured));
        eq("trustLevel: запись с пустым SSID пропускается",
                Logic.Trust.NONE,
                Logic.trustLevel(entries(new TrustedEntry("", ap)), "Home", ap, secured));
    }

    // --- security type ------------------------------------------------------

    private static void securityTypes() {
        // Значения android.net.wifi.WifiInfo.SECURITY_TYPE_* (API 30+).
        eq("securityType: OPEN", Logic.WifiSecurity.OPEN,
                Logic.wifiSecurityFromType(0));
        eq("securityType: WEP", Logic.WifiSecurity.SECURED,
                Logic.wifiSecurityFromType(1));
        eq("securityType: PSK", Logic.WifiSecurity.SECURED,
                Logic.wifiSecurityFromType(2));
        eq("securityType: EAP", Logic.WifiSecurity.SECURED,
                Logic.wifiSecurityFromType(3));
        eq("securityType: SAE", Logic.WifiSecurity.SECURED,
                Logic.wifiSecurityFromType(4));
        eq("securityType: OWE — без учётных данных", Logic.WifiSecurity.UNKNOWN,
                Logic.wifiSecurityFromType(6));
        eq("securityType: OSEN — без учётных данных", Logic.WifiSecurity.UNKNOWN,
                Logic.wifiSecurityFromType(10));
        eq("securityType: UNKNOWN (-1)", Logic.WifiSecurity.UNKNOWN,
                Logic.wifiSecurityFromType(-1));
        eq("securityType: отрицательное значение", Logic.WifiSecurity.UNKNOWN,
                Logic.wifiSecurityFromType(-42));
        eq("securityType: неизвестный будущий тип", Logic.WifiSecurity.UNKNOWN,
                Logic.wifiSecurityFromType(99));
        eq("securityType: DPP", Logic.WifiSecurity.SECURED,
                Logic.wifiSecurityFromType(13));

        // Значения android.net.wifi.WifiConfiguration.KeyMgmt.* (API 29).
        eq("keyMgmt: null", Logic.WifiSecurity.UNKNOWN,
                Logic.wifiSecurityFromKeyManagement(null));
        eq("keyMgmt: пустой набор", Logic.WifiSecurity.UNKNOWN,
                Logic.wifiSecurityFromKeyManagement(new BitSet()));
        eq("keyMgmt: только NONE — открытая сеть", Logic.WifiSecurity.OPEN,
                Logic.wifiSecurityFromKeyManagement(bits(0)));
        eq("keyMgmt: WPA2_PSK", Logic.WifiSecurity.SECURED,
                Logic.wifiSecurityFromKeyManagement(bits(4)));
        eq("keyMgmt: SAE", Logic.WifiSecurity.SECURED,
                Logic.wifiSecurityFromKeyManagement(bits(8)));
        eq("keyMgmt: OWE", Logic.WifiSecurity.UNKNOWN,
                Logic.wifiSecurityFromKeyManagement(bits(9)));
        eq("keyMgmt: OSEN", Logic.WifiSecurity.UNKNOWN,
                Logic.wifiSecurityFromKeyManagement(bits(5)));
        eq("keyMgmt: OWE в переходном режиме", Logic.WifiSecurity.UNKNOWN,
                Logic.wifiSecurityFromKeyManagement(bits(0, 9)));
        eq("keyMgmt: несколько защищённых методов", Logic.WifiSecurity.SECURED,
                Logic.wifiSecurityFromKeyManagement(bits(1, 8)));
        eq("keyMgmt: незнакомый бит", Logic.WifiSecurity.UNKNOWN,
                Logic.wifiSecurityFromKeyManagement(bits(30)));
    }

    private static BitSet bits(int... indexes) {
        BitSet set = new BitSet();
        for (int index : indexes) {
            set.set(index);
        }
        return set;
    }

    // --- parseTrustedEntries ------------------------------------------------

    private static void trustedLists() {
        List<TrustedEntry> single = Logic.parseTrustedEntries("Home\taa:bb:cc:dd:ee:ff");
        eqInt("parseTrustedEntries: одна запись", 1, single.size());
        eq("parseTrustedEntries: одна запись ssid", "Home", single.get(0).ssid);

        List<TrustedEntry> crlf = Logic.parseTrustedEntries(
                " Home\taa:bb:cc:dd:ee:ff \r\nWork\t11:22:33:44:55:66\r\n");
        eqInt("parseTrustedEntries: CRLF", 2, crlf.size());
        eq("parseTrustedEntries: CRLF вторая", "Work", crlf.get(1).ssid);

        List<TrustedEntry> cr = Logic.parseTrustedEntries("A\rB\rC");
        eqInt("parseTrustedEntries: одиночный CR", 3, cr.size());
        eq("parseTrustedEntries: CR последняя", "C", cr.get(2).ssid);

        List<TrustedEntry> blank = Logic.parseTrustedEntries(
                "\n\n Home \n\n\t\n Work\n\n");
        eqInt("parseTrustedEntries: пустые строки пропускаются", 2, blank.size());

        List<TrustedEntry> chars = Logic.parseTrustedEntries("Cafe, home;2\nOffice");
        eqInt("parseTrustedEntries: запятая и точка с запятой не разделяют", 2,
                chars.size());
        eq("parseTrustedEntries: SSID с запятой сохранён",
                "Cafe, home;2", chars.get(0).ssid);

        List<TrustedEntry> both = Logic.parseTrustedEntries(
                "Home | aa:bb:cc:dd:ee:ff\nLegacy");
        eqInt("parseTrustedEntries: UI-формат и legacy", 2, both.size());
        check("parseTrustedEntries: UI-запись привязана", both.get(0).isBound());
        check("parseTrustedEntries: legacy не привязана", !both.get(1).isBound());

        eqInt("parseTrustedEntries: null", 0, Logic.parseTrustedEntries(null).size());
        eqInt("parseTrustedEntries: пустая строка", 0, Logic.parseTrustedEntries("").size());
        eqInt("parseTrustedEntries: только переводы строк", 0,
                Logic.parseTrustedEntries("\r\n\r\r\n").size());
    }

    // --- networkKey / manualOffApplies -------------------------------------

    private static void networkKeys() {
        eq("networkKey: Wi-Fi без BSSID", "WIFI|Home|",
                Logic.networkKey(NetKind.WIFI, " Home "));
        eq("networkKey: SSID нормализуется в кавычках", "WIFI|Home|",
                Logic.networkKey(NetKind.WIFI, "\"Home\""));
        eq("networkKey: мобильная сеть", "MOBILE||",
                Logic.networkKey(NetKind.MOBILE, null));
        eq("networkKey: null kind", "NONE||", Logic.networkKey(null, null));
        eq("networkKey: разделитель внутри SSID не ломает ключ",
                "WIFI|A|B|", Logic.networkKey(NetKind.WIFI, "A|B"));
        check("networkKey: BSSID больше не входит в ключ",
                !Logic.networkKey(NetKind.WIFI, "Home").contains(":"));

        String home = Logic.networkKey(NetKind.WIFI, "Home");
        check("manualOffApplies: та же сеть", Logic.manualOffApplies(home, home));
        check("manualOffApplies: удобная форма по SSID",
                Logic.manualOffApplies(home, NetKind.WIFI, " Home "));
        check("manualOffApplies: другая Wi-Fi",
                !Logic.manualOffApplies(home, Logic.networkKey(NetKind.WIFI, "Work")));
        check("manualOffApplies: переход на мобильную",
                !Logic.manualOffApplies(home, Logic.networkKey(NetKind.MOBILE, null)));
        check("manualOffApplies: мобильная остаётся мобильной",
                Logic.manualOffApplies("MOBILE||", "MOBILE||"));
        check("manualOffApplies: SSID недоступен — запрет сохраняется",
                Logic.manualOffApplies(home, "WIFI||"));
        check("manualOffApplies: SSID появился после WIFI|| — запрет сохраняется",
                Logic.manualOffApplies("WIFI||", home));
        check("manualOffApplies: сохранённый SSID неизвестен — запрет консервативен",
                Logic.manualOffApplies("WIFI||", Logic.networkKey(NetKind.WIFI, "Work")));
        check("manualOffApplies: пустой сохранённый ключ",
                !Logic.manualOffApplies("", home));
        check("manualOffApplies: null сохранённый ключ",
                !Logic.manualOffApplies(null, home));
        check("manualOffApplies: текущий ключ null при сохранённом — сохраняем",
                Logic.manualOffApplies(home, null));
        check("manualOffApplies: ключ без типа сети не действует",
                !Logic.manualOffApplies("Home", home));
        check("manualOffApplies: SSID с разделителем",
                Logic.manualOffApplies("WIFI|A|B|", "WIFI|A|B|"));
        check("manualOffApplies: SSID с разделителем не путается с другим",
                !Logic.manualOffApplies("WIFI|A|B|", "WIFI|A|C|"));

        // Старый формат с BSSID: мигрирует к ключу уровня SSID.
        String legacyBound = "WIFI|Home|aa:bb:cc:dd:ee:ff";
        check("legacy-ключ с BSSID применяется к тому же SSID без BSSID",
                Logic.manualOffApplies(legacyBound, home));
        eq("manualOffKeyAfterCheck: миграция старого ключа с BSSID",
                home, Logic.manualOffKeyAfterCheck(legacyBound, home));
        check("legacy-ключ с BSSID применяется при роуминге на другую точку",
                Logic.manualOffApplies(legacyBound, "WIFI|Home|bb:bb:bb:bb:bb:bb"));
        eq("manualOffKeyAfterCheck: миграция при роуминге",
                home, Logic.manualOffKeyAfterCheck(legacyBound, "WIFI|Home|bb:bb:bb:bb:bb:bb"));
        eq("manualOffKeyAfterCheck: две legacy-точки одного SSID",
                home, Logic.manualOffKeyAfterCheck("WIFI|Home|aa:bb:cc:dd:ee:ff",
                        "WIFI|Home|bb:bb:bb:bb:bb:bb"));
        eq("manualOffKeyAfterCheck: текущий ключ null",
                home, Logic.manualOffKeyAfterCheck(home, null));
        eq("manualOffKeyAfterCheck: WIFI|| мигрирует при появлении SSID",
                home, Logic.manualOffKeyAfterCheck("WIFI||", home));
        eq("manualOffKeyAfterCheck: недоступный SSID не теряет сохранённый",
                home, Logic.manualOffKeyAfterCheck(home, "WIFI||"));
        eq("manualOffKeyAfterCheck: другая сеть снимает запрет",
                "", Logic.manualOffKeyAfterCheck(home, Logic.networkKey(NetKind.WIFI, "Work")));
        eq("manualOffKeyAfterCheck: смена типа сети снимает запрет",
                "", Logic.manualOffKeyAfterCheck(home, "MOBILE||"));
        eq("manualOffKeyAfterCheck: мобильная сеть остаётся",
                "MOBILE||", Logic.manualOffKeyAfterCheck("MOBILE||", "MOBILE||"));
        eq("manualOffKeyAfterCheck: пустой сохранённый ключ",
                "", Logic.manualOffKeyAfterCheck("", home));
    }

    // --- exitOffStep --------------------------------------------------------

    private static void exitOffSteps() {
        final long now = 1_000_000L;
        final long window = now + 12_000L;
        final int max = Logic.MAX_EXIT_OFF_ATTEMPTS;

        eq("exitOffStep: Tailscale не активен",
                Logic.ExitOffStep.DEFER,
                Logic.exitOffStep(false, true, false, 0L, now));
        eq("exitOffStep: Tailscale не активен, окно уже идёт",
                Logic.ExitOffStep.DEFER,
                Logic.exitOffStep(false, false, true, window, now));
        eq("exitOffStep: старт окна",
                Logic.ExitOffStep.START,
                Logic.exitOffStep(true, true, false, 0L, now));
        eq("exitOffStep: ждём восстановления exit node перед командой",
                Logic.ExitOffStep.WAIT,
                Logic.exitOffStep(true, true, false, window, now));
        eq("exitOffStep: окно истекло — пора отправлять",
                Logic.ExitOffStep.SEND,
                Logic.exitOffStep(true, true, false, now - 1L, now));
        eq("exitOffStep: окно истекло, exit node не активен — отправляем",
                Logic.ExitOffStep.SEND,
                Logic.exitOffStep(true, false, false, now - 1L, now));
        eq("exitOffStep: команда отправлена, ждём подтверждения",
                Logic.ExitOffStep.WAIT,
                Logic.exitOffStep(true, true, true, window, now));
        eq("exitOffStep: exit node исчез, но окно ещё идёт",
                Logic.ExitOffStep.WAIT,
                Logic.exitOffStep(true, false, true, window, now));
        eq("exitOffStep: exit node исчез и окно истекло",
                Logic.ExitOffStep.CONFIRMED,
                Logic.exitOffStep(true, false, true, now, now));
        eq("exitOffStep: exit node остался после окна",
                Logic.ExitOffStep.FAILED,
                Logic.exitOffStep(true, true, true, now - 1L, now));
        eq("exitOffStep: на границе окна оно уже истекло",
                Logic.ExitOffStep.FAILED,
                Logic.exitOffStep(true, true, true, window, window));
        eq("exitOffStep: повтор после паузы (sent=false, окно идёт)",
                Logic.ExitOffStep.WAIT,
                Logic.exitOffStep(true, true, false, window, now));
        eq("exitOffStep: повтор после паузы (sent=false, окно истекло)",
                Logic.ExitOffStep.SEND,
                Logic.exitOffStep(true, true, false, now, now));

        // Исчерпание попыток: после MAX автоматических SEND больше нет.
        eq("exitOffStep: последняя разрешённая попытка ещё повторяется",
                Logic.ExitOffStep.SEND,
                Logic.exitOffStep(true, true, false, now - 1L, now, max - 1, false));
        eq("exitOffStep: попытки исчерпаны перед отправкой",
                Logic.ExitOffStep.BLOCKED,
                Logic.exitOffStep(true, true, false, now - 1L, now, max, false));
        eq("exitOffStep: попытки исчерпаны после неудачи",
                Logic.ExitOffStep.BLOCKED,
                Logic.exitOffStep(true, true, true, now - 1L, now, max, false));
        eq("exitOffStep: блокировка не срабатывает раньше окна",
                Logic.ExitOffStep.WAIT,
                Logic.exitOffStep(true, true, true, window, now, max, false));
        eq("exitOffStep: в blocked команды не отправляются",
                Logic.ExitOffStep.BLOCKED,
                Logic.exitOffStep(true, true, false, now - 1L, now, max, true));
        eq("exitOffStep: blocked подтверждает исчезновение ноды",
                Logic.ExitOffStep.CONFIRMED,
                Logic.exitOffStep(true, false, false, 0L, now, max, true));
        eq("exitOffStep: blocked и Tailscale не активен — DEFER, а не SEND",
                Logic.ExitOffStep.DEFER,
                Logic.exitOffStep(false, false, false, 0L, now, max, true));
        eq("exitOffStep: успех важнее исчерпанных попыток",
                Logic.ExitOffStep.CONFIRMED,
                Logic.exitOffStep(true, false, true, now, now, max, false));
        eqInt("MAX_EXIT_OFF_ATTEMPTS", 5, max);

        exitOffLoop();
    }

    /**
     * Модель цикла контроллера: SEND → FAILED → пауза → SEND, пока попытки не
     * исчерпаны. Проверяет, что цикл конечен и после BLOCKED команд нет.
     */
    private static void exitOffLoop() {
        final int max = Logic.MAX_EXIT_OFF_ATTEMPTS;
        final long window = 12_000L;
        final long pause = 5L * 60L * 1000L;

        int attempts = 0;
        boolean sent = false;
        boolean blocked = false;
        boolean exitActive = true;
        long now = 0L;
        long deadline = 0L;
        int transitions = 0;
        Logic.ExitOffStep terminal = null;

        while (terminal == null && transitions++ < 100) {
            Logic.ExitOffStep step = Logic.exitOffStep(true, exitActive, sent,
                    deadline, now, attempts, blocked);
            switch (step) {
                case START:
                    deadline = now + window;
                    break;
                case SEND:
                    attempts++;
                    sent = true;
                    deadline = now + window;
                    break;
                case WAIT:
                    now = deadline;
                    break;
                case FAILED:
                    // Контроллер считает неудачу и сдаётся на MAX.
                    if (attempts >= max) {
                        blocked = true;
                    } else {
                        sent = false;
                        deadline = now + pause;
                        now = deadline;
                    }
                    break;
                case BLOCKED:
                    blocked = true;
                    terminal = step;
                    break;
                case CONFIRMED:
                    terminal = step;
                    break;
                default:
                    break;
            }
        }

        eq("цикл снятия: сдался после MAX попыток",
                Logic.ExitOffStep.BLOCKED, terminal);
        eqInt("цикл снятия: ровно MAX отправок", max, attempts);
        check("цикл снятия: конечное число переходов", transitions < 100);
        check("цикл снятия: долг не снят", blocked && exitActive);

        int afterBlock = attempts;
        boolean sendsAfterBlock = false;
        for (int i = 0; i < 50; i++) {
            Logic.ExitOffStep step = Logic.exitOffStep(true, true, false,
                    now - 1L, now + i, afterBlock, true);
            if (step == Logic.ExitOffStep.SEND) {
                sendsAfterBlock = true;
            }
        }
        check("после BLOCKED нет повторных SEND", !sendsAfterBlock);
        eq("после BLOCKED шаг остаётся BLOCKED",
                Logic.ExitOffStep.BLOCKED,
                Logic.exitOffStep(true, true, false, 0L, now, afterBlock, true));
        eq("BLOCKED снимается, когда нода исчезла",
                Logic.ExitOffStep.CONFIRMED,
                Logic.exitOffStep(true, false, false, 0L, now, afterBlock, true));

        // Успех до исчерпания: нода исчезла после второй отправки.
        int successAttempts = 0;
        boolean successSent = false;
        boolean successBlocked = false;
        boolean active = true;
        long successNow = 0L;
        long successDeadline = 0L;
        int guard = 0;
        Logic.ExitOffStep successStep = null;
        while (successStep == null && guard++ < 100) {
            Logic.ExitOffStep step = Logic.exitOffStep(true, active, successSent,
                    successDeadline, successNow, successAttempts, successBlocked);
            switch (step) {
                case START:
                    successDeadline = successNow + window;
                    break;
                case SEND:
                    successAttempts++;
                    successSent = true;
                    successDeadline = successNow + window;
                    break;
                case WAIT:
                    successNow = successDeadline;
                    if (successAttempts == 2) {
                        active = false;
                    }
                    break;
                case FAILED:
                    successSent = false;
                    successDeadline = successNow + pause;
                    successNow = successDeadline;
                    break;
                case BLOCKED:
                case CONFIRMED:
                    successStep = step;
                    break;
                default:
                    break;
            }
        }
        eq("цикл снятия: успех до исчерпания попыток",
                Logic.ExitOffStep.CONFIRMED, successStep);
        eqInt("цикл снятия: успех после двух отправок", 2, successAttempts);
    }

    // --- Цель exit node, долг снятия и владение -----------------------------

    private static void exitOffGoals() {
        // Правило требует exit node только на незнакомой Wi-Fi при заданном
        // имени и включённом управлении.
        check("ruleRequires: незнакомая Wi-Fi требует exit node",
                Logic.ruleRequiresExitNode(true, true, NetKind.WIFI, false));
        check("ruleRequires: доверенная Wi-Fi не требует",
                !Logic.ruleRequiresExitNode(true, true, NetKind.WIFI, true));
        check("ruleRequires: мобильная сеть не требует",
                !Logic.ruleRequiresExitNode(true, true, NetKind.MOBILE, false));
        check("ruleRequires: без имени не требует",
                !Logic.ruleRequiresExitNode(true, false, NetKind.WIFI, false));
        check("ruleRequires: управление выключено",
                !Logic.ruleRequiresExitNode(false, true, NetKind.WIFI, false));

        check("exitNodeWanted: правило и нет ручного запрета",
                Logic.exitNodeWanted(true, false));
        check("exitNodeWanted: ручной запрет важнее правила",
                !Logic.exitNodeWanted(true, true));
        check("exitNodeWanted: правило не требует",
                !Logic.exitNodeWanted(false, false));

        // Medium 1: смена сети на незнакомую Wi-Fi отменяет долг снятия.
        eq("offDebtAction: сеть не менялась — KEEP",
                Logic.OffDebtAction.KEEP,
                Logic.offDebtActionOnNetworkChange(false, true));
        eq("offDebtAction: незнакомая Wi-Fi — CANCEL",
                Logic.OffDebtAction.CANCEL,
                Logic.offDebtActionOnNetworkChange(true, true));
        eq("offDebtAction: сеть без правила — RESTART",
                Logic.OffDebtAction.RESTART,
                Logic.offDebtActionOnNetworkChange(true, false));

        // Сценарий Medium 1: pending_off стартовал на доверенной Wi-Fi,
        // затем сеть сменилась на незнакомую, где правило требует ON.
        boolean pendingOff = true;
        boolean offSent = false;
        Logic.OffDebtAction action = Logic.offDebtActionOnNetworkChange(
                true, Logic.exitNodeWanted(
                        Logic.ruleRequiresExitNode(true, true, NetKind.WIFI, false), false));
        if (action == Logic.OffDebtAction.CANCEL) {
            pendingOff = false;
            offSent = false;
        }
        check("Medium 1: долг снятия отменён на незнакомой Wi-Fi", !pendingOff);
        check("Medium 1: off не отправляется, правило включает ноду", !offSent);

        // Medium 2: Tailscale off + правило off — активная попытка
        // приостанавливается, владение сохраняется.
        check("shouldDefer: Tailscale off и правило off — отложить",
                Logic.shouldDeferExitOff(false, false));
        check("shouldDefer: Tailscale off, но правило ON — не блокируем ON",
                !Logic.shouldDeferExitOff(false, true));
        check("shouldDefer: Tailscale активен — снятие продолжается",
                !Logic.shouldDeferExitOff(true, false));

        boolean applied = true;
        String appliedName = "exit-node";
        boolean ownership = Logic.hasExitOffOwnership(applied, appliedName.length() > 0);
        check("ownership: applied и имя сохранены", ownership);
        check("ownership: только имя",
                Logic.hasExitOffOwnership(false, true));
        check("ownership: только applied",
                Logic.hasExitOffOwnership(true, false));
        check("ownership: пусто", !Logic.hasExitOffOwnership(false, false));

        // Tailscale off + правило off: pending снят, но владение осталось.
        boolean pending = true;
        if (Logic.shouldDeferExitOff(false, false)) {
            pending = false;
        }
        check("Medium 2: Tailscale off + правило off — pending снят", !pending);
        check("Medium 2: Tailscale off + правило off — владение сохранено",
                ownership);

        // Tailscale подключился позже на сети, где правило требует off:
        // отложенное снятие возобновляется по владению.
        check("Medium 2: Tailscale on и правило off — снятие возобновляется",
                Logic.shouldResumeOwnedOff(true, false, ownership));
        check("Medium 2: Tailscale off — не возобновляем",
                !Logic.shouldResumeOwnedOff(false, false, ownership));
        check("Medium 2: правило ON — не возобновляем",
                !Logic.shouldResumeOwnedOff(true, true, ownership));
        check("Medium 2: нет владения — не возобновляем",
                !Logic.shouldResumeOwnedOff(true, false, false));

        // Подтверждение исчезновения ноды — единственный момент очистки.
        boolean confirmed = false;
        boolean ownershipAfterConfirm = ownership;
        confirmed = Logic.exitOffStep(true, false, true, 1_000L, 1_000L)
                == Logic.ExitOffStep.CONFIRMED;
        if (confirmed) {
            ownershipAfterConfirm = false;
            applied = false;
            appliedName = "";
        }
        check("Medium 2: подтверждение снимает владение",
                confirmed && !ownershipAfterConfirm);
        check("Medium 2: владение очищено вместе с именем",
                !Logic.hasExitOffOwnership(applied, appliedName.length() > 0));

        // Новая незнакомая Wi-Fi не блокирует включение: DEFER не срабатывает,
        // даже если владение ещё хранится.
        check("Medium 2: незнакомая Wi-Fi не блокирует ON",
                !Logic.shouldDeferExitOff(false, true));
    }

    // --- Сетевой tick, долг и BSSID ----------------------------------------

    private static void networkTickPolicy() {
        // Medium: сетевые callback'и не запускают watchdog/retry с пробами
        // интернета. Они остаются только в штатном периодическом tick.
        check("сетевой tick: watchdog/retry не запускаются",
                !Logic.runsBackgroundChecks(Logic.TickSource.NETWORK));
        check("периодический tick: watchdog/retry запускаются",
                Logic.runsBackgroundChecks(Logic.TickSource.PERIODIC));

        // Medium 1 не должен регрессировать: сетевой путь по-прежнему
        // пересматривает долг и отменяет его на незнакомой Wi-Fi.
        eq("сетевой tick: незнакомая Wi-Fi отменяет долг снятия",
                Logic.OffDebtAction.CANCEL,
                Logic.offDebtActionOnNetworkChange(true,
                        Logic.exitNodeWanted(
                                Logic.ruleRequiresExitNode(true, true, NetKind.WIFI, false),
                                false)));

        // Low: долг снятия привязан к SSID+BSSID, поэтому смена точки с тем
        // же SSID — смена сети. Ручной запрет при этом остаётся на SSID.
        check("offBssidChanged: тот же BSSID в другом регистре",
                !Logic.offBssidChanged("AA:BB:CC:DD:EE:FF", "aa:bb:cc:dd:ee:ff"));
        check("offBssidChanged: другая точка того же SSID",
                Logic.offBssidChanged("aa:bb:cc:dd:ee:ff", "aa:bb:cc:dd:ee:00"));
        check("offBssidChanged: сохранённый BSSID неизвестен",
                !Logic.offBssidChanged("", "aa:bb:cc:dd:ee:ff"));
        check("offBssidChanged: текущий BSSID недоступен",
                !Logic.offBssidChanged("aa:bb:cc:dd:ee:ff", null));
        check("offBssidChanged: оба BSSID неизвестны",
                !Logic.offBssidChanged(null, ""));

        // Решение о смене сети складывается из SSID и BSSID: долг снятия
        // пересматривается, если изменился хотя бы один известный признак.
        check("offNetworkChanged: та же сеть",
                !Logic.offNetworkChanged(false, false));
        check("offNetworkChanged: сменился SSID",
                Logic.offNetworkChanged(true, false));
        check("offNetworkChanged: сменился BSSID у того же SSID",
                Logic.offNetworkChanged(false, true));
        check("offNetworkChanged: сменились SSID и BSSID",
                Logic.offNetworkChanged(true, true));
    }

    // --- Адресная очистка ошибок -------------------------------------------

    private static void errorSources() {
        check("errorSourceMatches: любая очистка (null)",
                Logic.errorSourceMatches("vpn", null));
        check("errorSourceMatches: тот же источник",
                Logic.errorSourceMatches("exit_node", "exit_node"));
        check("errorSourceMatches: другой источник не очищается",
                !Logic.errorSourceMatches("vpn", "exit_node"));
        check("errorSourceMatches: legacy без источника очищается",
                Logic.errorSourceMatches("", "exit_node"));
        check("errorSourceMatches: null-источник считается legacy",
                Logic.errorSourceMatches(null, "vpn"));
        check("errorSourceMatches: пустой запрос не совпадает с непустым",
                !Logic.errorSourceMatches("vpn", ""));
    }

    // --- maskHost -----------------------------------------------------------

    private static void hostMasking() {
        eq("maskHost(null)", "<empty>", Logic.maskHost(null));
        eq("maskHost(пусто)", "<empty>", Logic.maskHost(""));
        eq("maskHost(пробелы)", "<empty>", Logic.maskHost("   "));
        eq("maskHost(IPv4)", "203.***.***.***", Logic.maskHost("203.0.113.7"));
        eq("maskHost(IPv4 с пробелами)", "203.***.***.***", Logic.maskHost(" 203.0.113.7 "));
        eq("maskHost(IPv4 /8)", "10.***.***.***", Logic.maskHost("10.1.2.3"));
        eq("maskHost(не IPv4: октет > 255)", "9***", Logic.maskHost("999.1.1.1"));
        eq("maskHost(не IPv4: три октета)", "1***", Logic.maskHost("1.2.3"));
        eq("maskHost(не IPv4: пять октетов)", "1***", Logic.maskHost("1.2.3.4.5"));
        eq("maskHost(IPv4 с портом)", "1.2.3.4:…", Logic.maskHost("1.2.3.4:443"));
        eq("maskHost(IPv6)", "2001:…", Logic.maskHost("2001:db8::1"));
        eq("maskHost(IPv6 loopback)", ":…", Logic.maskHost("::1"));
        eq("maskHost(имя хоста)", "e***", Logic.maskHost("example.com"));
        eq("maskHost(хост с портом)", "example.com:…", Logic.maskHost("example.com:443"));
        eq("maskHost(имя из одного символа)", "l***", Logic.maskHost("localhost"));
    }

    // --- prefixMatches ------------------------------------------------------

    private static void prefixes() {
        byte[] subnet = bytes(192, 168, 1, 0);
        byte[] v6net = ipv6(0x20, 0x01, 0x0d, 0xb8, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0);
        byte[] v6host = v6net.clone();
        v6host[6] = 0x12;
        v6host[7] = 0x34;
        byte[] v6other = v6net.clone();
        v6other[3] = (byte) 0xb9;

        check("prefixMatches /24 нижняя граница",
                Logic.prefixMatches(subnet, 24, bytes(192, 168, 1, 0)));
        check("prefixMatches /24 верхняя граница",
                Logic.prefixMatches(subnet, 24, bytes(192, 168, 1, 255)));
        check("prefixMatches /24 другая подсеть",
                !Logic.prefixMatches(subnet, 24, bytes(192, 168, 2, 1)));
        check("prefixMatches /23 соседняя половина",
                Logic.prefixMatches(bytes(192, 168, 0, 0), 23, bytes(192, 168, 1, 255)));
        check("prefixMatches /23 за границей",
                !Logic.prefixMatches(bytes(192, 168, 0, 0), 23, bytes(192, 168, 2, 0)));
        check("prefixMatches /25 нижняя половина",
                Logic.prefixMatches(subnet, 25, bytes(192, 168, 1, 127)));
        check("prefixMatches /25 верхняя половина",
                !Logic.prefixMatches(subnet, 25, bytes(192, 168, 1, 128)));
        check("prefixMatches /31 пара",
                Logic.prefixMatches(bytes(192, 168, 1, 0), 31, bytes(192, 168, 1, 1)));
        check("prefixMatches /31 соседняя пара",
                !Logic.prefixMatches(bytes(192, 168, 1, 0), 31, bytes(192, 168, 1, 2)));
        check("prefixMatches /32 совпал",
                Logic.prefixMatches(bytes(192, 168, 1, 7), 32, bytes(192, 168, 1, 7)));
        check("prefixMatches /32 не совпал",
                !Logic.prefixMatches(bytes(192, 168, 1, 7), 32, bytes(192, 168, 1, 8)));

        check("prefixMatches /0", Logic.prefixMatches(bytes(0, 0, 0, 0), 0, bytes(10, 20, 30, 40)));
        check("prefixMatches /0 при разных адресах",
                Logic.prefixMatches(bytes(255, 255, 255, 255), 0, bytes(1, 2, 3, 4)));

        check("prefixMatches IPv6 /32", Logic.prefixMatches(v6net, 32, v6host));
        check("prefixMatches IPv6 /32 другой префикс",
                !Logic.prefixMatches(v6net, 32, v6other));
        check("prefixMatches IPv6 /0", Logic.prefixMatches(new byte[16], 0, v6host));
        check("prefixMatches IPv6 /128", Logic.prefixMatches(v6net, 128, v6net.clone()));
        check("prefixMatches IPv6 /128 отличие в младших битах",
                !Logic.prefixMatches(v6net, 128, v6host));

        check("prefixMatches null-сеть", !Logic.prefixMatches(null, 24, subnet));
        check("prefixMatches null-адрес", !Logic.prefixMatches(subnet, 24, null));
        check("prefixMatches разная длина", !Logic.prefixMatches(subnet, 24, v6host));
        check("prefixMatches отрицательный префикс", !Logic.prefixMatches(subnet, -1, subnet));
        check("prefixMatches префикс больше длины IPv4",
                !Logic.prefixMatches(subnet, 33, subnet));
        check("prefixMatches префикс больше длины IPv6",
                !Logic.prefixMatches(v6net, 129, v6host));
    }

    // --- routeWithin --------------------------------------------------------

    private static void routes() {
        byte[] privateNet = bytes(10, 0, 0, 0);
        byte[] v6net = ipv6(0x20, 0x01, 0x0d, 0xb8, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0);
        byte[] v6route = v6net.clone();
        v6route[6] = 0x12;
        v6route[7] = 0x34;
        byte[] v6other = v6net.clone();
        v6other[3] = (byte) 0xb9;

        check("routeWithin /16 внутри /8",
                Logic.routeWithin(bytes(10, 1, 0, 0), 16, privateNet, 8));
        check("routeWithin /8 равен /8",
                Logic.routeWithin(bytes(10, 0, 0, 0), 8, privateNet, 8));
        check("routeWithin другой адрес",
                !Logic.routeWithin(bytes(11, 0, 0, 0), 8, privateNet, 8));
        check("routeWithin маршрут шире цели",
                !Logic.routeWithin(bytes(10, 0, 0, 0), 16, bytes(10, 0, 0, 0), 24));
        check("routeWithin default внутри /0",
                Logic.routeWithin(bytes(192, 168, 0, 0), 16, bytes(0, 0, 0, 0), 0));
        check("routeWithin /32 внутри /24",
                Logic.routeWithin(bytes(10, 0, 0, 7), 32, privateNet, 24));
        check("routeWithin /32 вне /24",
                !Logic.routeWithin(bytes(10, 0, 1, 7), 32, privateNet, 24));
        check("routeWithin IPv6 /48 внутри /32",
                Logic.routeWithin(v6route, 48, v6net, 32));
        check("routeWithin IPv6 мимо /32",
                !Logic.routeWithin(v6other, 48, v6net, 32));

        check("routeWithin null-маршрут", !Logic.routeWithin(null, 8, privateNet, 8));
        check("routeWithin null-цель", !Logic.routeWithin(privateNet, 8, null, 8));
        check("routeWithin отрицательный префикс маршрута",
                !Logic.routeWithin(privateNet, -1, privateNet, 8));
        check("routeWithin префикс маршрута больше длины",
                !Logic.routeWithin(privateNet, 33, privateNet, 8));
        check("routeWithin отрицательный целевой префикс",
                !Logic.routeWithin(privateNet, 8, privateNet, -1));
        check("routeWithin разная длина адресов",
                !Logic.routeWithin(v6net, 48, privateNet, 8));
    }

    // --- targetFor ----------------------------------------------------------

    private static void targets() {
        eq("targetFor(null)", VpnType.NONE, Logic.targetFor(null, false));
        eq("targetFor(WIFI доверенная)", VpnType.NONE, Logic.targetFor(NetKind.WIFI, true));
        eq("targetFor(WIFI незнакомая)",
                VpnType.TAILSCALE, Logic.targetFor(NetKind.WIFI, false));
        eq("targetFor(MOBILE)", VpnType.WDTT, Logic.targetFor(NetKind.MOBILE, false));
        eq("targetFor(MOBILE, trusted игнорируется)",
                VpnType.WDTT, Logic.targetFor(NetKind.MOBILE, true));
        eq("targetFor(OTHER)", VpnType.NONE, Logic.targetFor(NetKind.OTHER, false));
        eq("targetFor(NONE)", VpnType.NONE, Logic.targetFor(NetKind.NONE, true));
    }

    // --- ruleSatisfied ------------------------------------------------------

    private static void rules() {
        check("ruleSatisfied(null kind)",
                !Logic.ruleSatisfied(null, true, VpnType.TAILSCALE, false, false, false));

        check("ruleSatisfied: доверенная Wi-Fi без VPN",
                Logic.ruleSatisfied(NetKind.WIFI, false, VpnType.NONE, true, false, false));
        check("ruleSatisfied: доверенная Wi-Fi с VPN",
                !Logic.ruleSatisfied(NetKind.WIFI, true, VpnType.TAILSCALE, true, false, false));
        check("ruleSatisfied: доверенная Wi-Fi игнорирует exit node",
                Logic.ruleSatisfied(NetKind.WIFI, false, VpnType.NONE, true, true, true));

        check("ruleSatisfied: незнакомая Wi-Fi + Tailscale",
                Logic.ruleSatisfied(NetKind.WIFI, true, VpnType.TAILSCALE, false, false, false));
        check("ruleSatisfied: exit не требуется, exitActive не важен",
                Logic.ruleSatisfied(NetKind.WIFI, true, VpnType.TAILSCALE, false, false, true));
        check("ruleSatisfied: exit активен",
                Logic.ruleSatisfied(NetKind.WIFI, true, VpnType.TAILSCALE, false, true, true));
        check("ruleSatisfied: exit не активен",
                !Logic.ruleSatisfied(NetKind.WIFI, true, VpnType.TAILSCALE, false, true, false));
        check("ruleSatisfied: незнакомая Wi-Fi + WDTT",
                !Logic.ruleSatisfied(NetKind.WIFI, true, VpnType.WDTT, false, false, false));
        check("ruleSatisfied: незнакомая Wi-Fi без VPN",
                !Logic.ruleSatisfied(NetKind.WIFI, false, VpnType.TAILSCALE, false, false, false));
        check("ruleSatisfied: тип null",
                !Logic.ruleSatisfied(NetKind.WIFI, true, null, false, false, false));

        check("ruleSatisfied: MOBILE + WDTT",
                Logic.ruleSatisfied(NetKind.MOBILE, true, VpnType.WDTT, false, false, false));
        check("ruleSatisfied: MOBILE + Tailscale",
                !Logic.ruleSatisfied(NetKind.MOBILE, true, VpnType.TAILSCALE, false, false, false));
        check("ruleSatisfied: MOBILE без VPN",
                !Logic.ruleSatisfied(NetKind.MOBILE, false, VpnType.WDTT, false, false, false));
        check("ruleSatisfied: OTHER",
                !Logic.ruleSatisfied(NetKind.OTHER, true, VpnType.WDTT, false, false, false));
        check("ruleSatisfied: NONE",
                !Logic.ruleSatisfied(NetKind.NONE, true, VpnType.WDTT, true, false, false));
    }

    // --- retryDelayMs -------------------------------------------------------

    private static void retryDelays() {
        eqInt("retryDelayMs(0) — как первая попытка",
                5 * MINUTE_MS, Logic.retryDelayMs(0));
        eqInt("retryDelayMs(-1)", 5 * MINUTE_MS, Logic.retryDelayMs(-1));
        eqInt("retryDelayMs(1)", 5 * MINUTE_MS, Logic.retryDelayMs(1));
        eqInt("retryDelayMs(2)", 10 * MINUTE_MS, Logic.retryDelayMs(2));
        eqInt("retryDelayMs(3)", 20 * MINUTE_MS, Logic.retryDelayMs(3));
        eqInt("retryDelayMs(4)", 30 * MINUTE_MS, Logic.retryDelayMs(4));
        eqInt("retryDelayMs(5)", 30 * MINUTE_MS, Logic.retryDelayMs(5));
        eqInt("retryDelayMs(100)", 30 * MINUTE_MS, Logic.retryDelayMs(100));
        eqInt("retryDelayMs(Integer.MAX_VALUE)",
                RETRY_CAP_MS, Logic.retryDelayMs(Integer.MAX_VALUE));

        boolean monotonic = true;
        boolean capped = true;
        int previous = 0;
        for (int attempt = 0; attempt <= 40; attempt++) {
            int delay = Logic.retryDelayMs(attempt);
            if (delay < previous) {
                monotonic = false;
            }
            if (delay > RETRY_CAP_MS) {
                capped = false;
            }
            previous = delay;
        }
        check("retryDelayMs: задержка не убывает", monotonic);
        check("retryDelayMs: потолок 30 минут", capped);
    }

    // --- withinWindow -------------------------------------------------------

    private static void windows() {
        final long now = 1_700_000_000_000L;

        check("withinWindow: момент сейчас, окно 0",
                Logic.withinWindow(now, now, 0L));
        check("withinWindow: секунда назад при окне 0",
                !Logic.withinWindow(now, now - 1L, 0L));
        check("withinWindow: ровно на границе окна",
                Logic.withinWindow(now, now - 60_000L, 60_000L));
        check("withinWindow: на миллисекунду старше окна",
                !Logic.withinWindow(now, now - 60_001L, 60_000L));
        check("withinWindow: момент в будущем",
                !Logic.withinWindow(now, now + 1L, 60_000L));
        check("withinWindow: at = 0",
                !Logic.withinWindow(now, 0L, 60_000L));
        check("withinWindow: отрицательный at",
                !Logic.withinWindow(now, -5L, 60_000L));
        check("withinWindow: отрицательное окно",
                !Logic.withinWindow(now, now, -1L));
        check("withinWindow: отрицательное окно и старый момент",
                !Logic.withinWindow(now, now - 1L, -1L));
        check("withinWindow: окно Long.MAX_VALUE",
                Logic.withinWindow(now, 1L, Long.MAX_VALUE));
    }

    // --- Хелперы ------------------------------------------------------------

    private static void check(String name, boolean condition) {
        checks++;
        if (!condition) {
            fail(name, "ожидалось true, получено false");
        }
    }

    private static void eq(String name, Object expected, Object actual) {
        checks++;
        boolean equal = expected == null ? actual == null : expected.equals(actual);
        if (!equal) {
            fail(name, "ожидалось <" + show(expected) + ">, получено <" + show(actual) + ">");
        }
    }

    private static void eqInt(String name, int expected, int actual) {
        checks++;
        if (expected != actual) {
            fail(name, "ожидалось " + expected + ", получено " + actual);
        }
    }

    private static void fail(String name, String message) {
        failures++;
        System.out.println("FAIL: " + name + ": " + message);
    }

    private static String show(Object value) {
        if (value == null) {
            return "null";
        }
        if (value instanceof String) {
            return '"' + ((String) value).replace("\t", "\\t") + '"';
        }
        return String.valueOf(value);
    }

    private static byte[] bytes(int... values) {
        byte[] result = new byte[values.length];
        for (int i = 0; i < values.length; i++) {
            result[i] = (byte) values[i];
        }
        return result;
    }

    private static byte[] ipv6(int... values) {
        if (values.length != 16) {
            throw new IllegalArgumentException("IPv6: нужно 16 байт, передано " + values.length);
        }
        return bytes(values);
    }

    private static List<TrustedEntry> entries(TrustedEntry... items) {
        List<TrustedEntry> list = new ArrayList<>();
        Collections.addAll(list, items);
        return list;
    }
}
