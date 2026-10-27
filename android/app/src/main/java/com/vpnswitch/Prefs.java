package com.vpnswitch;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.LinkedHashSet;
import java.util.Set;

/** Настройки и последний результат работы приложения. */
public final class Prefs {

    private static final String FILE_NAME = "vpn_switcher";

    private static final String KEY_AUTO_ENABLED = "auto_enabled";
    private static final String KEY_TRUSTED_SSIDS = "trusted_ssids";
    private static final String KEY_WDTT_TRUSTED = "wdtt_trusted";
    private static final String KEY_WDTT_PAUSED_ON_WIFI = "wdtt_paused_on_wifi";
    private static final String KEY_LAST_STATUS = "last_status";
    private static final String KEY_LAST_OK = "last_ok";
    private static final String KEY_LAST_TIME = "last_time";
    private static final String KEY_LAST_ERROR = "last_error";
    private static final String KEY_LAST_NET_KIND = "last_net_kind";
    private static final String KEY_LEFT_WIFI_AT = "left_wifi_at";

    private static final String KEY_EXIT_MANAGE_ENABLED = "exit_manage_enabled";
    private static final String KEY_EXIT_NODE_NAME = "exit_node_name";
    private static final String KEY_EXIT_NODE_IP = "exit_node_ip";
    private static final String KEY_EXIT_NODE_ALLOW_LAN = "exit_node_allow_lan";
    private static final String KEY_EXIT_WATCHDOG_ENABLED = "exit_watchdog_enabled";
    private static final String KEY_EXIT_NODE_APPLIED = "exit_node_applied";
    private static final String KEY_EXIT_NODE_PENDING_ON = "exit_node_pending_on";
    private static final String KEY_EXIT_RETRY_AT = "exit_retry_at";
    private static final String KEY_EXIT_FAIL_COUNT = "exit_fail_count";

    public static final String DEFAULT_EXIT_NODE_NAME = "";
    public static final String DEFAULT_EXIT_NODE_IP = "";

    private final SharedPreferences prefs;

    public Prefs(Context context) {
        prefs = context.getApplicationContext()
                .getSharedPreferences(FILE_NAME, Context.MODE_PRIVATE);
    }

    public boolean isAutoEnabled() {
        return prefs.getBoolean(KEY_AUTO_ENABLED, false);
    }

    public void setAutoEnabled(boolean enabled) {
        prefs.edit().putBoolean(KEY_AUTO_ENABLED, enabled).apply();
    }

    public boolean isWdttTrusted() {
        return prefs.getBoolean(KEY_WDTT_TRUSTED, true);
    }

    public void setWdttTrusted(boolean trusted) {
        prefs.edit().putBoolean(KEY_WDTT_TRUSTED, trusted).apply();
    }

    // --- Собственный список доверенных Wi-Fi -------------------------------

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

    /** Новая копия списка доверенных SSID без кавычек и пустых значений. */
    public Set<String> getTrustedSsids() {
        Set<String> result = new LinkedHashSet<>();
        Set<String> stored;
        try {
            stored = prefs.getStringSet(KEY_TRUSTED_SSIDS, null);
        } catch (Throwable t) {
            return result;
        }
        if (stored == null) {
            return result;
        }
        for (String raw : stored) {
            String ssid = normalizeSsid(raw);
            if (ssid.length() > 0) {
                result.add(ssid);
            }
        }
        return result;
    }

    /** Сохраняет нормализованный список; null означает пустой список. */
    public void setTrustedSsids(Set<String> ssids) {
        Set<String> normalized = new LinkedHashSet<>();
        if (ssids != null) {
            for (String raw : ssids) {
                String ssid = normalizeSsid(raw);
                if (ssid.length() > 0) {
                    normalized.add(ssid);
                }
            }
        }
        prefs.edit().putStringSet(KEY_TRUSTED_SSIDS, normalized).apply();
    }

    /** true, если SSID после нормализации точно совпадает с элементом списка. */
    public boolean isTrustedSsid(String ssid) {
        String normalized = normalizeSsid(ssid);
        return normalized.length() > 0 && getTrustedSsids().contains(normalized);
    }

    public void addTrustedSsid(String ssid) {
        String normalized = normalizeSsid(ssid);
        if (normalized.length() == 0) {
            return;
        }
        Set<String> ssids = getTrustedSsids();
        ssids.add(normalized);
        setTrustedSsids(ssids);
    }

    public void removeTrustedSsid(String ssid) {
        String normalized = normalizeSsid(ssid);
        if (normalized.length() == 0) {
            return;
        }
        Set<String> ssids = getTrustedSsids();
        if (ssids.remove(normalized)) {
            setTrustedSsids(ssids);
        }
    }

    /**
     * true, если WDTT Plus реально ушёл в паузу на доверенной Wi-Fi.
     * В этом состоянии toggle отправлять нельзя: он выключил бы паузу/восстановление.
     */
    public boolean isWdttPausedOnWifi() {
        return prefs.getBoolean(KEY_WDTT_PAUSED_ON_WIFI, false);
    }

    public void setWdttPausedOnWifi(boolean paused) {
        prefs.edit().putBoolean(KEY_WDTT_PAUSED_ON_WIFI, paused).apply();
    }

    public String getLastStatus() {
        return prefs.getString(KEY_LAST_STATUS, "");
    }

    public boolean isLastOk() {
        return prefs.getBoolean(KEY_LAST_OK, true);
    }

    public long getLastTime() {
        return prefs.getLong(KEY_LAST_TIME, 0L);
    }

    public String getLastError() {
        return prefs.getString(KEY_LAST_ERROR, "");
    }

    /** Последний известный тип сети; при отсутствии или ошибке разбора — NONE. */
    public NetKind getLastNetKind() {
        String value = prefs.getString(KEY_LAST_NET_KIND, null);
        if (value == null || value.length() == 0) {
            return NetKind.NONE;
        }
        try {
            return NetKind.valueOf(value);
        } catch (Throwable t) {
            return NetKind.NONE;
        }
    }

    public void setLastNetKind(NetKind kind) {
        prefs.edit().putString(KEY_LAST_NET_KIND,
                (kind == null ? NetKind.NONE : kind).name()).apply();
    }

    /** Время (System.currentTimeMillis), когда приложение зафиксировало уход с Wi-Fi. */
    public long getLeftWifiAt() {
        return prefs.getLong(KEY_LEFT_WIFI_AT, 0L);
    }

    public void setLeftWifiAt(long timeMs) {
        prefs.edit().putLong(KEY_LEFT_WIFI_AT, timeMs).apply();
    }

    public boolean hasLeftWifiRecently(long windowMs) {
        long at = getLeftWifiAt();
        if (at <= 0L) {
            return false;
        }
        long diff = System.currentTimeMillis() - at;
        return diff >= 0L && diff <= windowMs;
    }

    // --- Exit node Tailscale -------------------------------------------------

    public boolean isExitManageEnabled() {
        return prefs.getBoolean(KEY_EXIT_MANAGE_ENABLED, false);
    }

    public void setExitManageEnabled(boolean enabled) {
        prefs.edit().putBoolean(KEY_EXIT_MANAGE_ENABLED, enabled).apply();
    }

    /** Имя ноды как в списке Exit Nodes Tailscale; пустая строка допустима (но не применяется). */
    public String getExitNodeName() {
        String value = prefs.getString(KEY_EXIT_NODE_NAME, null);
        return value == null ? DEFAULT_EXIT_NODE_NAME : value;
    }

    public void setExitNodeName(String name) {
        prefs.edit().putString(KEY_EXIT_NODE_NAME, name == null ? "" : name.trim()).apply();
    }

    /** Tailnet-IP сервера для TCP-проверки перед возвратом exit node. */
    public String getExitNodeIp() {
        String value = prefs.getString(KEY_EXIT_NODE_IP, null);
        return value == null ? DEFAULT_EXIT_NODE_IP : value;
    }

    public void setExitNodeIp(String ip) {
        prefs.edit().putString(KEY_EXIT_NODE_IP, ip == null ? "" : ip.trim()).apply();
    }

    public boolean isExitNodeAllowLan() {
        return prefs.getBoolean(KEY_EXIT_NODE_ALLOW_LAN, false);
    }

    public void setExitNodeAllowLan(boolean allow) {
        prefs.edit().putBoolean(KEY_EXIT_NODE_ALLOW_LAN, allow).apply();
    }

    public boolean isExitWatchdogEnabled() {
        return prefs.getBoolean(KEY_EXIT_WATCHDOG_ENABLED, true);
    }

    public void setExitWatchdogEnabled(boolean enabled) {
        prefs.edit().putBoolean(KEY_EXIT_WATCHDOG_ENABLED, enabled).apply();
    }

    /** true, если последняя команда, отправленная приложением, включала exit node. */
    public boolean isExitNodeApplied() {
        return prefs.getBoolean(KEY_EXIT_NODE_APPLIED, false);
    }

    public void setExitNodeApplied(boolean applied) {
        prefs.edit().putBoolean(KEY_EXIT_NODE_APPLIED, applied).apply();
    }

    /** true, если exit node нужно включить, как только подключится Tailscale. */
    public boolean getExitNodePendingOn() {
        return prefs.getBoolean(KEY_EXIT_NODE_PENDING_ON, false);
    }

    public void setExitNodePendingOn(boolean pending) {
        prefs.edit().putBoolean(KEY_EXIT_NODE_PENDING_ON, pending).apply();
    }

    /** Время (System.currentTimeMillis) следующей попытки вернуть exit node; 0 — не запланирована. */
    public long getExitRetryAt() {
        return prefs.getLong(KEY_EXIT_RETRY_AT, 0L);
    }

    public void setExitRetryAt(long timeMs) {
        prefs.edit().putLong(KEY_EXIT_RETRY_AT, Math.max(0L, timeMs)).apply();
    }

    /** Число подряд неудачных проверок интернета для watchdog. */
    public int getExitFailCount() {
        return Math.max(0, prefs.getInt(KEY_EXIT_FAIL_COUNT, 0));
    }

    public void setExitFailCount(int count) {
        prefs.edit().putInt(KEY_EXIT_FAIL_COUNT, Math.max(0, count)).apply();
    }

    /** Сохраняет последний статус, результат и время. error == null очищает прошлую ошибку. */
    public void record(String status, boolean ok, String error) {
        SharedPreferences.Editor editor = prefs.edit()
                .putString(KEY_LAST_STATUS, status == null ? "" : status)
                .putBoolean(KEY_LAST_OK, ok)
                .putLong(KEY_LAST_TIME, System.currentTimeMillis());
        if (error == null || error.length() == 0) {
            editor.remove(KEY_LAST_ERROR);
        } else {
            editor.putString(KEY_LAST_ERROR, error);
        }
        editor.apply();
    }
}
