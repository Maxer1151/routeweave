package com.vpnswitch;

import com.vpnswitch.Logic.TrustedEntry;

import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
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

        List<TrustedEntry> bound = entries(new TrustedEntry("Home", ap));
        eq("trustLevel: точное совпадение",
                Logic.Trust.TRUSTED, Logic.trustLevel(bound, "Home", ap, false));
        eq("trustLevel: SSID и BSSID в разном регистре/пробелах",
                Logic.Trust.TRUSTED,
                Logic.trustLevel(entries(new TrustedEntry("\" Home \"", "AA:BB:CC:DD:EE:FF")),
                        " Home ", "AA:BB:CC:DD:EE:FF", false));
        eq("trustLevel: SSID совпал, BSSID другой",
                Logic.Trust.MISMATCH, Logic.trustLevel(bound, "Home", otherAp, false));
        eq("trustLevel: текущий BSSID служебный",
                Logic.Trust.MISMATCH,
                Logic.trustLevel(bound, "Home", "00:00:00:00:00:00", false));
        eq("trustLevel: текущий BSSID недоступен",
                Logic.Trust.MISMATCH, Logic.trustLevel(bound, "Home", null, false));

        List<TrustedEntry> legacy = entries(new TrustedEntry("Home", null));
        eq("trustLevel: legacy без BSSID",
                Logic.Trust.UNBOUND, Logic.trustLevel(legacy, "Home", ap, false));
        eq("trustLevel: legacy без BSSID и текущий BSSID недоступен",
                Logic.Trust.UNBOUND, Logic.trustLevel(legacy, "Home", null, false));

        eq("trustLevel: пустой список",
                Logic.Trust.NONE, Logic.trustLevel(entries(), "Home", ap, false));
        eq("trustLevel: null вместо списка",
                Logic.Trust.NONE, Logic.trustLevel(null, "Home", ap, false));
        eq("trustLevel: другой SSID",
                Logic.Trust.NONE, Logic.trustLevel(bound, "Other", ap, false));
        eq("trustLevel: открытая сеть",
                Logic.Trust.NONE, Logic.trustLevel(bound, "Home", ap, true));
        eq("trustLevel: пустой SSID",
                Logic.Trust.NONE, Logic.trustLevel(bound, "", ap, false));
        eq("trustLevel: null SSID",
                Logic.Trust.NONE, Logic.trustLevel(bound, null, ap, false));

        eq("trustLevel: несколько точек, совпала вторая",
                Logic.Trust.TRUSTED,
                Logic.trustLevel(entries(new TrustedEntry("Home", otherAp),
                        new TrustedEntry("Home", ap)), "Home", ap, false));
        eq("trustLevel: совпавшая точка важнее legacy",
                Logic.Trust.TRUSTED,
                Logic.trustLevel(entries(new TrustedEntry("Home", null),
                        new TrustedEntry("Home", ap)), "Home", ap, false));
        eq("trustLevel: чужая точка и legacy дают UNBOUND",
                Logic.Trust.UNBOUND,
                Logic.trustLevel(entries(new TrustedEntry("Home", otherAp),
                        new TrustedEntry("Home", null)), "Home", ap, false));
        eq("trustLevel: null в списке пропускается",
                Logic.Trust.TRUSTED,
                Logic.trustLevel(entries((TrustedEntry) null, new TrustedEntry("Home", ap)),
                        "Home", ap, false));
        eq("trustLevel: запись с пустым SSID пропускается",
                Logic.Trust.NONE,
                Logic.trustLevel(entries(new TrustedEntry("", ap)), "Home", ap, false));
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
