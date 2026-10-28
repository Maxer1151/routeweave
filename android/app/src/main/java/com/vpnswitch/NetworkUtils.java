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
import android.net.wifi.WifiConfiguration;
import android.net.wifi.WifiInfo;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.PowerManager;
import android.os.SystemClock;
import android.app.NotificationManager;
import android.util.Log;

import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.util.BitSet;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

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
        WifiInfo info = wifiInfo(context);
        if (info == null) {
            return null;
        }
        String ssid = visibleSsid(info.getSSID());
        if (ssid == null) {
            Log.d(TAG, "currentSsid: SSID недоступен — " + ssidUnavailableReason(context));
        }
        return ssid;
    }

    /**
     * Текущий BSSID в нижнем регистре. Те же ограничения, что у SSID: нужно
     * разрешение на местоположение и включённая геолокация. null — сеть не
     * Wi-Fi, BSSID недоступен или пришло служебное значение.
     */
    public static String currentBssid(Context context) {
        if (context == null || !hasSsidPermission(context) || !isLocationEnabled(context)) {
            return null;
        }
        if (currentKind(context) != NetKind.WIFI) {
            return null;
        }
        WifiInfo info = wifiInfo(context);
        return info == null ? null : Logic.normalizeBssid(info.getBSSID());
    }

    /**
     * Тип защиты текущей Wi-Fi. На API 30+ используется
     * {@link WifiInfo#getCurrentSecurityType()}, на API 29 — сохранённая
     * конфигурация по networkId. Любая неопределённость даёт UNKNOWN:
     * неизвестную сеть нельзя считать ни открытой, ни защищённой, поэтому
     * доверие по ней не подтверждается (fail-closed).
     */
    @SuppressWarnings("deprecation")
    public static Logic.WifiSecurity currentWifiSecurity(Context context) {
        if (context == null || currentKind(context) != NetKind.WIFI) {
            return Logic.WifiSecurity.UNKNOWN;
        }
        return wifiSecurity(context, wifiInfo(context));
    }

    /**
     * Один снимок Wi-Fi для решения о доверии: SSID, BSSID, открытость и уровень
     * доверия. Позволяет не сканировать сеть несколько раз в одном решении.
     */
    public static final class WifiAccess {
        public final String ssid;
        public final String bssid;
        public final Logic.WifiSecurity security;
        public final Logic.Trust trust;

        public WifiAccess(String ssid, String bssid, Logic.WifiSecurity security,
                          Logic.Trust trust) {
            this.ssid = ssid;
            this.bssid = bssid;
            this.security = security == null ? Logic.WifiSecurity.UNKNOWN : security;
            this.trust = trust == null ? Logic.Trust.NONE : trust;
        }

        public boolean isOpen() {
            return security == Logic.WifiSecurity.OPEN;
        }

        public static WifiAccess none() {
            return new WifiAccess(null, null, Logic.WifiSecurity.UNKNOWN, Logic.Trust.NONE);
        }
    }

    /** Компактный снимок текущей Wi-Fi; вне Wi-Fi — пустой снимок. */
    public static WifiAccess currentWifiAccess(Context context) {
        return currentWifiAccess(context, currentKind(context));
    }

    /**
     * Снимок текущей Wi-Fi с уже известным типом сети: избавляет от повторного
     * getAllNetworks, когда kind посчитан вызывающим.
     */
    public static WifiAccess currentWifiAccess(Context context, NetKind knownKind) {
        if (context == null || knownKind != NetKind.WIFI) {
            return WifiAccess.none();
        }
        WifiInfo info = wifiInfo(context);
        String ssid = null;
        String bssid = null;
        if (info != null && hasSsidPermission(context) && isLocationEnabled(context)) {
            ssid = visibleSsid(info.getSSID());
            bssid = Logic.normalizeBssid(info.getBSSID());
        }
        Logic.WifiSecurity security = wifiSecurity(context, info);
        Logic.Trust trust = new Prefs(context).trustLevel(ssid, bssid, security);
        Log.d(TAG, "currentWifiAccess: ssid=" + (ssid == null ? "нет" : "есть")
                + ", bssid=" + (bssid == null ? "нет" : "есть")
                + ", security=" + security + ", trust=" + trust);
        return new WifiAccess(ssid, bssid, security, trust);
    }

    /** true, если сейчас Wi-Fi и её пара SSID+BSSID доверенная. */
    public static boolean isTrustedWifi(Context context) {
        return currentWifiAccess(context).trust == Logic.Trust.TRUSTED;
    }

    @SuppressWarnings("deprecation")
    private static WifiInfo wifiInfo(Context context) {
        try {
            WifiManager wm = (WifiManager) context.getApplicationContext()
                    .getSystemService(Context.WIFI_SERVICE);
            return wm == null ? null : wm.getConnectionInfo();
        } catch (Throwable t) {
            Log.d(TAG, "wifiInfo: " + t);
            return null;
        }
    }

    /** SSID без кавычек или null для скрытого/служебного значения. */
    private static String visibleSsid(String raw) {
        String ssid = Prefs.normalizeSsid(raw);
        if (ssid.length() == 0
                || "<unknown ssid>".equalsIgnoreCase(ssid)
                || ssid.startsWith("0x")
                || ssid.startsWith("0X")) {
            return null;
        }
        return ssid;
    }

    @SuppressWarnings("deprecation")
    private static Logic.WifiSecurity wifiSecurity(Context context, WifiInfo info) {
        if (info == null) {
            return Logic.WifiSecurity.UNKNOWN;
        }
        if (Build.VERSION.SDK_INT >= 30) {
            try {
                int type = info.getCurrentSecurityType();
                if (type == WifiInfo.SECURITY_TYPE_OPEN) {
                    return Logic.WifiSecurity.OPEN;
                }
                // Отрицательное значение — «тип неизвестен»: не угадываем.
                return type < 0 ? Logic.WifiSecurity.UNKNOWN : Logic.WifiSecurity.SECURED;
            } catch (Throwable t) {
                Log.d(TAG, "wifiSecurity: security type недоступен: " + t);
                return Logic.WifiSecurity.UNKNOWN;
            }
        }
        // API 29: отдельного security type у WifiInfo нет, смотрим конфигурацию.
        // Пустой или недоступный список означает «не знаем», а не «закрыта».
        try {
            WifiManager wm = (WifiManager) context.getApplicationContext()
                    .getSystemService(Context.WIFI_SERVICE);
            if (wm == null) {
                return Logic.WifiSecurity.UNKNOWN;
            }
            List<WifiConfiguration> configured = wm.getConfiguredNetworks();
            if (configured == null || configured.isEmpty()) {
                return Logic.WifiSecurity.UNKNOWN;
            }
            int networkId = info.getNetworkId();
            for (WifiConfiguration config : configured) {
                if (config == null || config.networkId != networkId) {
                    continue;
                }
                BitSet allowed = config.allowedKeyManagement;
                if (allowed == null || allowed.isEmpty()) {
                    return Logic.WifiSecurity.OPEN;
                }
                return allowed.cardinality() == 1
                        && allowed.get(WifiConfiguration.KeyMgmt.NONE)
                        ? Logic.WifiSecurity.OPEN : Logic.WifiSecurity.SECURED;
            }
        } catch (Throwable t) {
            Log.d(TAG, "wifiSecurity: конфигурация недоступна: " + t);
        }
        return Logic.WifiSecurity.UNKNOWN;
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

        return classifyVpn(context, cm, vpnNetwork);
    }

    /**
     * Классифицирует конкретную VPN-сеть. Если ownerUid (API 30+) дал известный
     * пакет, тип определяется только по нему: Tailscale, WDTT или OTHER.
     * Адресные и маршрутные эвристики применяются лишь при неизвестном owner —
     * иначе чужой VPN с похожими адресами был бы принят за Tailscale/WDTT.
     */
    private static VpnState classifyVpn(Context context, ConnectivityManager cm,
                                        Network vpnNetwork) {
        String ownerPackage = null;
        VpnType type = VpnType.NONE;
        String method = "none";

        // Способ 1: UID владельца VPN-сети (API 30+). Может быть недоступен на части прошивок.
        if (Build.VERSION.SDK_INT >= 30) {
            boolean ownerKnown = false;
            try {
                NetworkCapabilities caps = cm.getNetworkCapabilities(vpnNetwork);
                if (caps != null) {
                    int uid = caps.getOwnerUid();
                    if (uid >= 0) {
                        PackageManager pm = context.getPackageManager();
                        String[] packages = pm.getPackagesForUid(uid);
                        if (packages != null && packages.length > 0) {
                            ownerKnown = true;
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
                            if (type == VpnType.NONE) {
                                // Известный чужой пакет: сразу OTHER, эвристики ниже
                                // не должны переопределять тип.
                                type = VpnType.OTHER;
                                ownerPackage = packages[0];
                            }
                            method = "owner";
                        }
                    }
                }
            } catch (Throwable t) {
                // getOwnerUid() на некоторых устройствах кидает UnsupportedOperationException.
                Log.d(TAG, "detectVpn: getOwnerUid недоступен: " + t);
            }
            Log.d(TAG, "detectVpn: owner " + (ownerKnown ? "известен" : "неизвестен")
                    + ", type=" + type);
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

        Log.d(TAG, "detectVpn: active=true, type=" + type + ", method=" + method);
        return new VpnState(true, type, ownerPackage);
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
        return Logic.prefixMatches(target, prefix, address);
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
            return Logic.routeWithin(destination.getAddress().getAddress(),
                    destination.getPrefixLength(), target, targetPrefix);
        } catch (Throwable t) {
            return false;
        }
    }

    private static final long INSTALLED_CACHE_TTL_MS = 45000L;

    private static final Object INSTALLED_LOCK = new Object();
    private static final Map<String, InstalledEntry> INSTALLED_CACHE = new HashMap<>();

    private static final class InstalledEntry {
        final boolean installed;
        final long at;

        InstalledEntry(boolean installed, long at) {
            this.installed = installed;
            this.at = at;
        }
    }

    /**
     * Установлен ли пакет. Результат кэшируется на 45 с: UI и сервис проверяют
     * Tailscale и WDTT несколько раз за тик, а getPackageInfo дорог. Сброс кэша —
     * {@link #invalidateInstalledCache()} (вызывается в onResume экрана).
     */
    public static boolean isInstalled(Context context, String pkg) {
        if (context == null || pkg == null) {
            return false;
        }
        long now = SystemClock.elapsedRealtime();
        synchronized (INSTALLED_LOCK) {
            InstalledEntry cached = INSTALLED_CACHE.get(pkg);
            if (cached != null && now - cached.at < INSTALLED_CACHE_TTL_MS) {
                return cached.installed;
            }
        }
        return isInstalled(context, pkg, now);
    }

    private static boolean isInstalled(Context context, String pkg, long now) {
        boolean installed;
        try {
            context.getPackageManager().getPackageInfo(pkg, 0);
            installed = true;
        } catch (Throwable t) {
            installed = false;
        }
        synchronized (INSTALLED_LOCK) {
            INSTALLED_CACHE.put(pkg, new InstalledEntry(installed, now));
        }
        return installed;
    }

    /** Сбрасывает кэш установки пакетов: после возврата в приложение или установки. */
    public static void invalidateInstalledCache() {
        synchronized (INSTALLED_LOCK) {
            INSTALLED_CACHE.clear();
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

    /**
     * VPN-сеть заданного типа. Классификация — та же, что в {@link #detectVpn}:
     * ownerUid, адреса интерфейса, известные маршруты. Активная сеть в приоритете.
     */
    public static Network findVpnNetwork(Context context, VpnType type) {
        if (context == null || type == null || type == VpnType.NONE) {
            return null;
        }
        try {
            ConnectivityManager cm =
                    (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm == null) {
                return null;
            }
            Network active = cm.getActiveNetwork();
            Network first = null;
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
                    VpnState state = classifyVpn(context, cm, network);
                    if (state.type != type) {
                        continue;
                    }
                    if (active != null && active.equals(network)) {
                        return network;
                    }
                    if (first == null) {
                        first = network;
                    }
                } catch (Throwable t) {
                    Log.d(TAG, "findVpnNetwork(type): capabilities: " + t);
                }
            }
            return first;
        } catch (Throwable t) {
            Log.d(TAG, "findVpnNetwork(type): " + t);
            return null;
        }
    }

    /**
     * true, если у VPN-сети есть маршрут по умолчанию (IPv4 или IPv6).
     * Exit node Tailscale объявляет 0.0.0.0/0 и ::/0; без него — только
     * служебные маршруты, поэтому проверять нужно именно сеть Tailscale.
     */
    @SuppressWarnings("deprecation")
    public static boolean hasDefaultRoute(Context context) {
        Network tailscale = findVpnNetwork(context, VpnType.TAILSCALE);
        return hasDefaultRoute(context, tailscale);
    }

    /** true, если в указанной сети есть маршрут по умолчанию (IPv4 или IPv6). */
    @SuppressWarnings("deprecation")
    public static boolean hasDefaultRoute(Context context, Network network) {
        if (context == null || network == null) {
            return false;
        }
        try {
            ConnectivityManager cm =
                    (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm == null) {
                return false;
            }
            LinkProperties lp = cm.getLinkProperties(network);
            if (lp == null) {
                return false;
            }
            List<RouteInfo> routes = lp.getRoutes();
            if (routes == null) {
                return false;
            }
            for (RouteInfo route : routes) {
                if (route != null && route.isDefaultRoute()) {
                    Log.d(TAG, "hasDefaultRoute: default route в сети " + network);
                    return true;
                }
            }
        } catch (Throwable t) {
            Log.d(TAG, "hasDefaultRoute(network): " + t);
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
            Log.d(TAG, "probeTcp: " + Logic.maskHost(host) + ":" + port + " доступен");
            return true;
        } catch (Throwable t) {
            Log.d(TAG, "probeTcp: " + Logic.maskHost(host) + ":" + port
                    + " недоступен: " + t);
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
                Log.d(TAG, "probeInternet: интернет есть (" + Logic.maskHost(target.host)
                        + ":" + target.port + ")");
                return true;
            }
        }
        Log.d(TAG, "probeInternet: интернета нет");
        return false;
    }

    /**
     * Можно ли сейчас выполнять сетевые TCP-пробы: экран включён и устройство
     * не в Doze. В фоне пробы будят радио и легко дают ложные сбои.
     */
    public static boolean backgroundProbesAllowed(Context context) {
        if (context == null) {
            return false;
        }
        try {
            PowerManager pm = (PowerManager) context.getSystemService(Context.POWER_SERVICE);
            if (pm == null) {
                return false;
            }
            if (!pm.isInteractive()) {
                return false;
            }
            return !pm.isDeviceIdleMode();
        } catch (Throwable t) {
            Log.d(TAG, "backgroundProbesAllowed: " + t);
            return false;
        }
    }

    /**
     * true, если хотя бы одна физическая (не VPN) сеть помечена системой как
     * NET_CAPABILITY_VALIDATED. Если сведения недоступны — false: лучше не
     * засчитывать неудачу watchdog, чем снять exit node из-за сломанной сети.
     */
    public static boolean hasValidatedUnderlyingNetwork(Context context) {
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
                            || caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
                        continue;
                    }
                    if (caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)) {
                        return true;
                    }
                } catch (Throwable t) {
                    Log.d(TAG, "hasValidatedUnderlyingNetwork: capabilities: " + t);
                }
            }
        } catch (Throwable t) {
            Log.d(TAG, "hasValidatedUnderlyingNetwork: " + t);
        }
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
}
