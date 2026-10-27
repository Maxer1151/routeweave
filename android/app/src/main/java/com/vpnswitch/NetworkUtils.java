package com.vpnswitch;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.location.LocationManager;
import android.net.ConnectivityManager;
import android.net.IpPrefix;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.RouteInfo;
import android.net.wifi.WifiInfo;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.PowerManager;
import android.app.NotificationManager;
import android.util.Log;

import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.util.List;

/**
 * Определение типа сети и активного VPN исключительно средствами framework API.
 * Никаких обращений к чужим приложениям, кроме запросов PackageManager.
 */
public final class NetworkUtils {

    private static final String TAG = "VpnSwitcher";

    public static final String PKG_TAILSCALE = "com.tailscale.ipn";
    public static final String PKG_WDTT = "com.wdtt.plus";

    /* Маршруты, которые устанавливают VPN-клиенты (используются как fallback). */
    private static final byte[] TS_ROUTE_V4 = new byte[]{100, 64, 0, 0};
    private static final int TS_PREFIX_V4 = 10;
    private static final byte[] TS_ROUTE_V6 = new byte[]{
            (byte) 0xfd, 0x7a, 0x11, 0x5c, (byte) 0xa1, (byte) 0xe0,
            0, 0, 0, 0, 0, 0, 0, 0, 0, 0
    };
    private static final int TS_PREFIX_V6 = 48;

    private static final byte[] WDTT_ROUTE_V4 = new byte[]{10, 66, 66, 0};
    private static final int WDTT_PREFIX_V4 = 24;

    private NetworkUtils() {
    }

    /**
     * Wi-Fi имеет приоритет: если подключён Wi-Fi (даже без интернета) — WIFI.
     * Сети с транспортом VPN при определении типа доступа игнорируются.
     */
    public static NetKind currentKind(Context context) {
        if (context == null) {
            return NetKind.NONE;
        }
        ConnectivityManager cm =
                (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
        if (cm == null) {
            Log.d(TAG, "currentKind: ConnectivityManager недоступен");
            return NetKind.NONE;
        }

        boolean wifi = false;
        boolean cellular = false;
        boolean anyNonVpn = false;

        try {
            Network[] networks = cm.getAllNetworks();
            if (networks != null) {
                for (Network network : networks) {
                    try {
                        NetworkCapabilities caps = cm.getNetworkCapabilities(network);
                        if (caps == null) {
                            continue;
                        }
                        if (caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
                            continue;
                        }
                        anyNonVpn = true;
                        if (caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) {
                            wifi = true;
                        } else if (caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)) {
                            cellular = true;
                        }
                    } catch (Throwable t) {
                        Log.d(TAG, "currentKind: не удалось прочитать capabilities: " + t);
                    }
                }
            }
        } catch (Throwable t) {
            Log.d(TAG, "currentKind: ошибка getAllNetworks: " + t);
        }

        if (wifi) {
            return NetKind.WIFI;
        }
        if (cellular) {
            return NetKind.MOBILE;
        }
        if (anyNonVpn) {
            return NetKind.OTHER;
        }
        return NetKind.NONE;
    }

    /**
     * Разрешение, без которого система не отдаёт SSID: ACCESS_FINE_LOCATION на всех
     * версиях Android. NEARBY_WIFI_DEVICES для SSID недостаточно — SSID остаётся
     * location-sensitive полем даже на Android 13+.
     */
    public static boolean hasSsidPermission(Context context) {
        if (context == null) {
            return false;
        }
        try {
            return context.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                    == PackageManager.PERMISSION_GRANTED;
        } catch (Throwable t) {
            Log.d(TAG, "hasSsidPermission: " + t);
            return false;
        }
    }

    /**
     * Разрешение «Разрешить всегда»: без него Android применяет к SSID ограничение
     * while-in-use и в фоне (в том числе из foreground-сервиса без типа location)
     * отдаёт замаскированное имя сети.
     */
    public static boolean hasBackgroundLocationPermission(Context context) {
        if (context == null) {
            return false;
        }
        try {
            return context.checkSelfPermission(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
                    == PackageManager.PERMISSION_GRANTED;
        } catch (Throwable t) {
            Log.d(TAG, "hasBackgroundLocationPermission: " + t);
            return false;
        }
    }

    /** Короткая причина, почему SSID недоступен. Для логов и диагностики. */
    public static String ssidUnavailableReason(Context context) {
        if (context == null) {
            return "нет контекста";
        }
        if (!hasSsidPermission(context)) {
            return "нет разрешения на местоположение";
        }
        if (!isLocationEnabled(context)) {
            return "геолокация выключена";
        }
        if (!hasBackgroundLocationPermission(context)) {
            return "нет доступа к местоположению в фоне";
        }
        return "SSID скрыт системой";
    }

    /**
     * true, если системная геолокация включена. Без неё Android накладывает на SSID
     * маску ({@code <unknown ssid>}), даже если разрешение выдано.
     */
    public static boolean isLocationEnabled(Context context) {
        if (context == null) {
            return false;
        }
        try {
            LocationManager lm =
                    (LocationManager) context.getSystemService(Context.LOCATION_SERVICE);
            return lm != null && lm.isLocationEnabled();
        } catch (Throwable t) {
            Log.d(TAG, "isLocationEnabled: " + t);
            return false;
        }
    }

    /**
     * Текущий SSID без кавычек. null, если нет разрешения, геолокация выключена,
     * сеть не Wi-Fi, SSID скрыт ("<unknown ssid>") или пришёл в виде "0x...".
     */
    @SuppressWarnings("deprecation")
    public static String currentSsid(Context context) {
        if (context == null || !hasSsidPermission(context)) {
            return null;
        }
        if (currentKind(context) != NetKind.WIFI) {
            return null;
        }
        if (!isLocationEnabled(context)) {
            Log.d(TAG, "currentSsid: геолокация выключена");
            return null;
        }
        try {
            WifiManager wm = (WifiManager) context.getApplicationContext()
                    .getSystemService(Context.WIFI_SERVICE);
            if (wm == null) {
                return null;
            }
            WifiInfo info = wm.getConnectionInfo();
            if (info == null) {
                return null;
            }
            String ssid = Prefs.normalizeSsid(info.getSSID());
            if (ssid.length() == 0
                    || "<unknown ssid>".equalsIgnoreCase(ssid)
                    || ssid.startsWith("0x")
                    || ssid.startsWith("0X")) {
                Log.d(TAG, "currentSsid: SSID недоступен — " + ssidUnavailableReason(context));
                return null;
            }
            return ssid;
        } catch (Throwable t) {
            Log.d(TAG, "currentSsid: " + t);
            return null;
        }
    }

    /** true, если сейчас Wi-Fi и его SSID есть в списке доверенных приложения. */
    public static boolean isTrustedWifi(Context context) {
        if (context == null || currentKind(context) != NetKind.WIFI) {
            return false;
        }
        String ssid = currentSsid(context);
        if (ssid == null) {
            return false;
        }
        return new Prefs(context).isTrustedSsid(ssid);
    }

    /**
     * Ищет активный VPN. Сначала пытается определить владельца через getOwnerUid()
     * (API 30+), затем по адресам интерфейса и в конце — по известным маршрутам
     * (Tailscale CGNAT/ULA, WDTT 10.66.66.0/24).
     */
    public static VpnState detectVpn(Context context) {
        if (context == null) {
            return VpnState.none();
        }
        ConnectivityManager cm =
                (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
        if (cm == null) {
            return VpnState.none();
        }

        Network vpnNetwork = null;
        try {
            Network active = cm.getActiveNetwork();
            Network[] networks = cm.getAllNetworks();
            if (networks != null) {
                for (Network network : networks) {
                    try {
                        NetworkCapabilities caps = cm.getNetworkCapabilities(network);
                        if (caps == null
                                || !caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
                            continue;
                        }
                        if (vpnNetwork == null) {
                            vpnNetwork = network;
                        }
                        if (active != null && active.equals(network)) {
                            vpnNetwork = network;
                            break;
                        }
                    } catch (Throwable t) {
                        Log.d(TAG, "detectVpn: capabilities сети: " + t);
                    }
                }
            }
        } catch (Throwable t) {
            Log.d(TAG, "detectVpn: ошибка перечисления сетей: " + t);
            return VpnState.none();
        }

        if (vpnNetwork == null) {
            return VpnState.none();
        }

        String ownerPackage = null;
        VpnType type = VpnType.NONE;
        String method = "none";

        // Способ 1: UID владельца VPN-сети (API 30+). Может быть недоступен на части прошивок.
        if (Build.VERSION.SDK_INT >= 30) {
            try {
                NetworkCapabilities caps = cm.getNetworkCapabilities(vpnNetwork);
                if (caps != null) {
                    int uid = caps.getOwnerUid();
                    if (uid >= 0) {
                        PackageManager pm = context.getPackageManager();
                        String[] packages = pm.getPackagesForUid(uid);
                        if (packages != null) {
                            for (String pkg : packages) {
                                if (PKG_TAILSCALE.equals(pkg)) {
                                    type = VpnType.TAILSCALE;
                                    ownerPackage = pkg;
                                    break;
                                }
                                if (PKG_WDTT.equals(pkg)) {
                                    type = VpnType.WDTT;
                                    ownerPackage = pkg;
                                    break;
                                }
                            }
                            if (ownerPackage == null && packages.length > 0) {
                                ownerPackage = packages[0];
                            }
                        }
                        Log.d(TAG, "detectVpn: ownerUid=" + uid + ", packages="
                                + (packages == null ? "null" : join(packages)));
                    }
                }
            } catch (Throwable t) {
                // getOwnerUid() на некоторых устройствах кидает UnsupportedOperationException.
                Log.d(TAG, "detectVpn: getOwnerUid недоступен: " + t);
            }
            if (type != VpnType.NONE) {
                method = "owner";
            }
        } else {
            Log.d(TAG, "detectVpn: ownerUid недоступен (API " + Build.VERSION.SDK_INT
                    + " < 30), используем адреса и маршруты");
        }

        // Способ 2 (основной): адреса VPN-интерфейса. Префикс самого LinkAddress не важен,
        // сравниваются только биты адреса.
        if (type == VpnType.NONE) {
            try {
                LinkProperties lp = cm.getLinkProperties(vpnNetwork);
                if (lp != null) {
                    boolean tailscale = linkAddressMatches(lp, TS_ROUTE_V4, TS_PREFIX_V4)
                            || linkAddressMatches(lp, TS_ROUTE_V6, TS_PREFIX_V6);
                    boolean wdtt = linkAddressMatches(lp, WDTT_ROUTE_V4, WDTT_PREFIX_V4);
                    if (wdtt) {
                        type = VpnType.WDTT;
                    } else if (tailscale) {
                        type = VpnType.TAILSCALE;
                    }
                    if (type != VpnType.NONE) {
                        method = "address";
                    }
                }
            } catch (Throwable t) {
                Log.d(TAG, "detectVpn: адреса интерфейса недоступны: " + t);
            }
        }

        // Способ 3 (fallback): известные маршруты VPN.
        if (type == VpnType.NONE) {
            boolean tailscale = false;
            boolean wdtt = false;
            try {
                LinkProperties lp = cm.getLinkProperties(vpnNetwork);
                if (lp != null) {
                    List<RouteInfo> routes = lp.getRoutes();
                    if (routes != null) {
                        for (RouteInfo route : routes) {
                            if (routeMatches(route, TS_ROUTE_V4, TS_PREFIX_V4)
                                    || routeMatches(route, TS_ROUTE_V6, TS_PREFIX_V6)) {
                                tailscale = true;
                            }
                            if (routeMatches(route, WDTT_ROUTE_V4, WDTT_PREFIX_V4)) {
                                wdtt = true;
                            }
                        }
                    }
                }
            } catch (Throwable t) {
                Log.d(TAG, "detectVpn: маршруты недоступны: " + t);
            }
            if (wdtt) {
                type = VpnType.WDTT;
                method = "route";
            } else if (tailscale) {
                type = VpnType.TAILSCALE;
                method = "route";
            }
        }

        if (type == VpnType.NONE) {
            type = VpnType.OTHER;
        }

        VpnState state = new VpnState(true, type, ownerPackage);
        Log.d(TAG, "detectVpn: active=true, type=" + type + ", method=" + method
                + ", owner=" + ownerPackage);
        return state;
    }

    /** Проверяет, что один из адресов интерфейса попадает в заданный префикс. */
    private static boolean linkAddressMatches(LinkProperties lp, byte[] target, int prefix) {
        if (lp == null) {
            return false;
        }
        try {
            List<LinkAddress> addresses = lp.getLinkAddresses();
            if (addresses == null) {
                return false;
            }
            for (LinkAddress linkAddress : addresses) {
                if (linkAddress == null) {
                    continue;
                }
                InetAddress address = linkAddress.getAddress();
                if (address == null) {
                    continue;
                }
                if (prefixMatches(address.getAddress(), target, prefix)) {
                    return true;
                }
            }
        } catch (Throwable t) {
            Log.d(TAG, "linkAddressMatches: " + t);
        }
        return false;
    }

    /** Проверяет, что первые prefix бит адреса совпадают с целевыми. */
    private static boolean prefixMatches(byte[] address, byte[] target, int prefix) {
        if (address == null || target == null || address.length != target.length) {
            return false;
        }
        if (prefix < 0 || prefix > address.length * 8) {
            return false;
        }
        int fullBytes = prefix / 8;
        for (int i = 0; i < fullBytes; i++) {
            if (address[i] != target[i]) {
                return false;
            }
        }
        int remainder = prefix % 8;
        if (remainder != 0) {
            int mask = (0xFF << (8 - remainder)) & 0xFF;
            if ((address[fullBytes] & mask) != (target[fullBytes] & mask)) {
                return false;
            }
        }
        return true;
    }

    /** Проверяет, что маршрут целиком лежит внутри заданного префикса. */
    private static boolean routeMatches(RouteInfo route, byte[] target, int targetPrefix) {
        if (route == null) {
            return false;
        }
        try {
            IpPrefix destination = route.getDestination();
            if (destination == null) {
                return false;
            }
            byte[] address = destination.getAddress().getAddress();
            int routePrefix = destination.getPrefixLength();
            if (routePrefix < targetPrefix) {
                return false;
            }
            return prefixMatches(address, target, targetPrefix);
        } catch (Throwable t) {
            return false;
        }
    }

    public static boolean isInstalled(Context context, String pkg) {
        if (context == null || pkg == null) {
            return false;
        }
        try {
            context.getPackageManager().getPackageInfo(pkg, 0);
            return true;
        } catch (Throwable t) {
            return false;
        }
    }

    /**
     * Активная сеть устройства: через неё проверяется интернет.
     * Может быть VPN-сетью, если exit node стал сетью по умолчанию.
     */
    public static Network activeNetwork(Context context) {
        if (context == null) {
            return null;
        }
        try {
            ConnectivityManager cm =
                    (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
            return cm == null ? null : cm.getActiveNetwork();
        } catch (Throwable t) {
            Log.d(TAG, "activeNetwork: " + t);
            return null;
        }
    }

    /** VPN-сеть, предпочитая активную. В системе одновременно активен только один VPN. */
    @SuppressWarnings("deprecation")
    public static Network findVpnNetwork(Context context) {
        if (context == null) {
            return null;
        }
        try {
            ConnectivityManager cm =
                    (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm == null) {
                return null;
            }
            Network active = cm.getActiveNetwork();
            Network vpn = null;
            Network[] networks = cm.getAllNetworks();
            if (networks == null) {
                return null;
            }
            for (Network network : networks) {
                try {
                    NetworkCapabilities caps = cm.getNetworkCapabilities(network);
                    if (caps == null
                            || !caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
                        continue;
                    }
                    if (vpn == null) {
                        vpn = network;
                    }
                    if (active != null && active.equals(network)) {
                        return network;
                    }
                } catch (Throwable t) {
                    Log.d(TAG, "findVpnNetwork: capabilities: " + t);
                }
            }
            return vpn;
        } catch (Throwable t) {
            Log.d(TAG, "findVpnNetwork: " + t);
            return null;
        }
    }

    /** true, если у VPN-сети есть маршрут по умолчанию (IPv4 или IPv6). */
    @SuppressWarnings("deprecation")
    public static boolean hasDefaultRoute(Context context) {
        if (context == null) {
            return false;
        }
        try {
            ConnectivityManager cm =
                    (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm == null) {
                return false;
            }
            Network[] networks = cm.getAllNetworks();
            if (networks == null) {
                return false;
            }
            for (Network network : networks) {
                try {
                    NetworkCapabilities caps = cm.getNetworkCapabilities(network);
                    if (caps == null
                            || !caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
                        continue;
                    }
                    LinkProperties lp = cm.getLinkProperties(network);
                    if (lp == null) {
                        continue;
                    }
                    List<RouteInfo> routes = lp.getRoutes();
                    if (routes == null) {
                        continue;
                    }
                    for (RouteInfo route : routes) {
                        if (route != null && route.isDefaultRoute()) {
                            Log.d(TAG, "hasDefaultRoute: default route в VPN-сети " + network);
                            return true;
                        }
                    }
                } catch (Throwable t) {
                    Log.d(TAG, "hasDefaultRoute: " + t);
                }
            }
        } catch (Throwable t) {
            Log.d(TAG, "hasDefaultRoute: " + t);
        }
        return false;
    }

    /**
     * TCP-подключение к host:port через указанную сеть. Только для фоновых потоков:
     * connect() блокируется до timeoutMs. Любая ошибка — false.
     */
    public static boolean probeTcp(Network network, String host, int port, int timeoutMs) {
        if (host == null || host.length() == 0 || port <= 0) {
            return false;
        }
        Socket socket = null;
        try {
            socket = new Socket();
            if (network != null) {
                network.bindSocket(socket);
            }
            socket.connect(new InetSocketAddress(host, port), Math.max(1, timeoutMs));
            Log.d(TAG, "probeTcp: " + host + ":" + port + " доступен");
            return true;
        } catch (Throwable t) {
            Log.d(TAG, "probeTcp: " + host + ":" + port + " недоступен: " + t);
            return false;
        } finally {
            if (socket != null) {
                try {
                    socket.close();
                } catch (Throwable ignored) {
                    // закрытие сокета не влияет на результат
                }
            }
        }
    }

    /** Пара «хост:порт» для проверки интернета. */
    private static final class ProbeTarget {
        final String host;
        final int port;

        ProbeTarget(String host, int port) {
            this.host = host;
            this.port = port;
        }
    }

    private static final ProbeTarget[] INTERNET_PROBE_TARGETS = {
            // Yandex DNS по TCP: в РФ работает стабильно и доступен через VPS-выход.
            new ProbeTarget("77.88.8.8", 53),
            // ya.ru (Яндекс) по HTTPS: стабилен из РФ, в том числе через зарубежный VPS.
            new ProbeTarget("87.250.250.242", 443),
            // Google DNS по TCP: резервный адрес, обычно не фильтруется.
            new ProbeTarget("8.8.8.8", 53),
            // Cloudflare: дополнительный адрес, не единственный.
            new ProbeTarget("1.1.1.1", 443),
    };
    private static final int INTERNET_PROBE_TIMEOUT_MS = 2000;

    /**
     * Проверка интернета через активную сеть (при включённом exit node — через него).
     * Успех, если открылся хотя бы один адрес из списка. Только фоновый поток.
     *
     * Адреса выбраны под РФ и одновременно под выход через VPS: 77.88.8.8:53 и
     * 87.250.250.242:443 стабильны из РФ, 8.8.8.8:53 и 1.1.1.1:443 проверяют
     * внешний маршрут. TCP 53 (DNS) обычно не блокируется, в отличие от
     * 77.88.8.8:443; 1.1.1.1:443 оставлен только как дополнительный.
     */
    public static boolean probeInternet(Context context) {
        Network network = activeNetwork(context);
        if (network == null) {
            Log.d(TAG, "probeInternet: активной сети нет");
            return false;
        }
        for (ProbeTarget target : INTERNET_PROBE_TARGETS) {
            if (probeTcp(network, target.host, target.port, INTERNET_PROBE_TIMEOUT_MS)) {
                Log.d(TAG, "probeInternet: интернет есть (" + target.host + ":"
                        + target.port + ")");
                return true;
            }
        }
        Log.d(TAG, "probeInternet: интернета нет");
        return false;
    }

    /** true, если система оптимизирует батарею приложения (то есть разрешение не выдано). */
    public static boolean isBatteryOptimized(Context context) {
        if (context == null) {
            return true;
        }
        try {
            PowerManager pm = (PowerManager) context.getSystemService(Context.POWER_SERVICE);
            if (pm == null) {
                return true;
            }
            return !pm.isIgnoringBatteryOptimizations(context.getPackageName());
        } catch (Throwable t) {
            Log.d(TAG, "isBatteryOptimized: " + t);
            return true;
        }
    }

    public static boolean notificationsEnabled(Context context) {
        if (context == null) {
            return false;
        }
        try {
            NotificationManager nm =
                    (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
            return nm != null && nm.areNotificationsEnabled();
        } catch (Throwable t) {
            Log.d(TAG, "notificationsEnabled: " + t);
            return true;
        }
    }

    public static String kindLabel(NetKind kind) {
        if (kind == null) {
            return "Нет сети";
        }
        switch (kind) {
            case WIFI:
                return "Wi-Fi";
            case MOBILE:
                return "Моб. сеть";
            case OTHER:
                return "Другая сеть";
            case NONE:
            default:
                return "Нет сети";
        }
    }

    public static String vpnLabel(VpnType type) {
        if (type == null) {
            return "VPN не подключён";
        }
        switch (type) {
            case TAILSCALE:
                return "Tailscale";
            case WDTT:
                return "WDTT Plus";
            case OTHER:
                return "Другой VPN";
            case NONE:
            default:
                return "VPN не подключён";
        }
    }

    public static String vpnLabel(VpnState state) {
        if (state == null || !state.active || state.type == VpnType.NONE) {
            return "VPN не подключён";
        }
        if (state.type == VpnType.OTHER && state.ownerPackage != null) {
            return "Другой VPN (" + state.ownerPackage + ")";
        }
        return vpnLabel(state.type);
    }

    private static String join(String[] values) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < values.length; i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append(values[i]);
        }
        return sb.toString();
    }
}
