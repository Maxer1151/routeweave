package com.vpnswitch;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.Collection;
import java.util.LinkedHashSet;
import java.util.Set;

/** Настройки и последний результат работы приложения. */
public final class Prefs {

    private static final String FILE_NAME = "vpn_switcher";

    private static final String KEY_AUTO_ENABLED = "auto_enabled";
    private static final String KEY_TRUSTED_SSIDS = "trusted_ssids";
    private static final String KEY_WDTT_TRUSTED = "wdtt_trusted";
    private static final String KEY_WDTT_PAUSED_ON_WIFI = "wdtt_paused_on_wifi";
    private static final String KEY_WDTT_ACTIVE_ON_WIFI = "wdtt_active_on_wifi";
    private static final String KEY_LAST_STATUS = "last_status";
    private static final String KEY_LAST_OK = "last_ok";
    private static final String KEY_LAST_TIME = "last_time";
    private static final String KEY_LAST_ERROR = "last_error";
    private static final String KEY_LAST_NET_KIND = "last_net_kind";
    private static final String KEY_LEFT_WIFI_AT = "left_wifi_at";

    private static final String KEY_EXIT_MANAGE_ENABLED = "exit_manage_enabled";
    private static final String KEY_EXIT_NODE_NAME = "exit_node_name";
    private static final String KEY_EXIT_NODE_APPLIED_NAME = "exit_node_applied_name";
    private static final String KEY_EXIT_NODE_IP = "exit_node_ip";
    private static final String KEY_EXIT_NODE_ALLOW_LAN = "exit_node_allow_lan";
    private static final String KEY_EXIT_WATCHDOG_ENABLED = "exit_watchdog_enabled";
    private static final String KEY_EXIT_NODE_APPLIED = "exit_node_applied";
    private static final String KEY_EXIT_NODE_PENDING_ON = "exit_node_pending_on";
    private static final String KEY_EXIT_RETRY_AT = "exit_retry_at";
    private static final String KEY_EXIT_FAIL_COUNT = "exit_fail_count";
    private static final String KEY_EXIT_RETRY_COUNT = "exit_retry_count";
    private static final String KEY_EXIT_RETRY_BLOCKED = "exit_retry_blocked";
    private static final String KEY_EXIT_CONFIRM_AT = "exit_confirm_at";
    private static final String KEY_EXIT_PENDING_OFF = "exit_node_pending_off";
    private static final String KEY_EXIT_OFF_DEADLINE = "exit_off_deadline";
    private static final String KEY_EXIT_OFF_SENT = "exit_off_sent";
    private static final String KEY_EXIT_MANUAL_OFF_KEY = "exit_manual_off_key";

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
        return Logic.normalizeSsid(ssid);
    }

    /**
     * Доверенные сети как пары SSID+BSSID. Старые записи без BSSID читаются
     * как {@link Logic.Trust#UNBOUND} и не считаются доверенными (fail-closed).
     * Несколько точек одного SSID — несколько записей: так поддерживаются mesh
     * и роуминг между точками.
     */
    public Set<Logic.TrustedEntry> getTrustedNetworks() {
        Set<Logic.TrustedEntry> result = new LinkedHashSet<>();
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
            Logic.TrustedEntry entry = Logic.parseTrustedEntry(raw);
            if (entry != null) {
                result.add(entry);
            }
        }
        return result;
    }

    /** Сохраняет записи в формате {@code ssid + "\t" + bssid}; null — пустой список. */
    public void setTrustedNetworks(Collection<Logic.TrustedEntry> entries) {
        Set<String> formatted = new LinkedHashSet<>();
        if (entries != null) {
            for (Logic.TrustedEntry entry : entries) {
                if (entry == null) {
                    continue;
                }
                String raw = Logic.formatTrustedEntry(entry.ssid, entry.bssid);
                if (raw.length() > 0) {
                    formatted.add(raw);
                }
            }
        }
        prefs.edit().putStringSet(KEY_TRUSTED_SSIDS, formatted).apply();
    }

    /**
     * Добавляет пару SSID+BSSID. Если для этого SSID была старая запись без
     * привязки к точке, она заменяется привязанной. true — список изменился.
     */
    public boolean addTrustedNetwork(String ssid, String bssid) {
        String normalizedSsid = Logic.normalizeSsid(ssid);
        if (normalizedSsid.length() == 0) {
            return false;
        }
        String normalizedBssid = Logic.normalizeBssid(bssid);
        Set<Logic.TrustedEntry> entries = getTrustedNetworks();
        if (normalizedBssid != null) {
            entries.remove(new Logic.TrustedEntry(normalizedSsid, ""));
        }
        boolean changed = entries.add(new Logic.TrustedEntry(normalizedSsid, normalizedBssid));
        if (changed) {
            setTrustedNetworks(entries);
        }
        return changed;
    }

    /** Уровень доверия по паре SSID+BSSID текущей сети. */
    public Logic.Trust trustLevel(String ssid, String bssid, Logic.WifiSecurity security) {
        return Logic.trustLevel(getTrustedNetworks(), ssid, bssid, security);
    }

    /** Уникальные SSID из списка доверенных сетей. Для счётчика записей есть getTrustedNetworks(). */
    public Set<String> getTrustedSsids() {
        Set<String> result = new LinkedHashSet<>();
        for (Logic.TrustedEntry entry : getTrustedNetworks()) {
            if (entry.ssid.length() > 0) {
                result.add(entry.ssid);
            }
        }
        return result;
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

    /**
     * true, если WDTT был активен, пока приложение находилось на Wi-Fi. Флаг
     * ставится при наблюдении активного WDTT и переживает его авто-паузу: по
     * нему уход с Wi-Fi помечает паузу, не полагаясь на момент callback'а.
     */
    public boolean isWdttActiveOnWifi() {
        return prefs.getBoolean(KEY_WDTT_ACTIVE_ON_WIFI, false);
    }

    public void setWdttActiveOnWifi(boolean active) {
        prefs.edit().putBoolean(KEY_WDTT_ACTIVE_ON_WIFI, active).apply();
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
        return Logic.withinWindow(System.currentTimeMillis(), getLeftWifiAt(), windowMs);
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

    /**
     * Имя ноды, для которой приложение отправило команду включения. Хранится
     * отдельно от текущего имени: пользователь может переименовать ноду, пока
     * идёт подтверждение, а снять или заменить нужно именно применённую.
     */
    public String getExitNodeAppliedName() {
        String value = prefs.getString(KEY_EXIT_NODE_APPLIED_NAME, null);
        return value == null ? "" : value;
    }

    public void setExitNodeAppliedName(String name) {
        prefs.edit().putString(KEY_EXIT_NODE_APPLIED_NAME,
                name == null ? "" : name.trim()).apply();
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

    /** Номер попытки автовосстановления exit node (для backoff 5/10/20/30 мин). */
    public int getExitRetryCount() {
        return Math.max(0, prefs.getInt(KEY_EXIT_RETRY_COUNT, 0));
    }

    public void setExitRetryCount(int count) {
        prefs.edit().putInt(KEY_EXIT_RETRY_COUNT, Math.max(0, count)).apply();
    }

    /**
     * true, если автовосстановление exit node остановлено: исчерпаны попытки,
     * нет IP сервера для проверки или Tailscale не принимает команду. Правило
     * Wi-Fi в этом состоянии не перезапускает команду — нужен ручной запуск.
     */
    public boolean isExitRetryBlocked() {
        return prefs.getBoolean(KEY_EXIT_RETRY_BLOCKED, false);
    }

    public void setExitRetryBlocked(boolean blocked) {
        prefs.edit().putBoolean(KEY_EXIT_RETRY_BLOCKED, blocked).apply();
    }

    /** Время (System.currentTimeMillis) подтверждения включения exit node; 0 — не ждём. */
    public long getExitConfirmAt() {
        return prefs.getLong(KEY_EXIT_CONFIRM_AT, 0L);
    }

    public void setExitConfirmAt(long timeMs) {
        prefs.edit().putLong(KEY_EXIT_CONFIRM_AT, Math.max(0L, timeMs)).apply();
    }

    /**
     * true, если приложение отправило (или должно отправить) снятие exit node,
     * но фактическое снятие ещё не подтверждено. Владение (applied/applied_name)
     * в это время сохраняется: Tailscale мог восстановить ноду после
     * переподключения.
     */
    public boolean isExitPendingOff() {
        return prefs.getBoolean(KEY_EXIT_PENDING_OFF, false);
    }

    public void setExitPendingOff(boolean pending) {
        prefs.edit().putBoolean(KEY_EXIT_PENDING_OFF, pending).apply();
    }

    /** Дедлайн окна проверки снятия; 0 — окно ещё не начато. */
    public long getExitOffDeadline() {
        return prefs.getLong(KEY_EXIT_OFF_DEADLINE, 0L);
    }

    public void setExitOffDeadline(long timeMs) {
        prefs.edit().putLong(KEY_EXIT_OFF_DEADLINE, Math.max(0L, timeMs)).apply();
    }

    /** true, если команда снятия уже отправлена в текущем окне. */
    public boolean isExitOffSent() {
        return prefs.getBoolean(KEY_EXIT_OFF_SENT, false);
    }

    public void setExitOffSent(boolean sent) {
        prefs.edit().putBoolean(KEY_EXIT_OFF_SENT, sent).apply();
    }

    /**
     * Ключ сети, на которой пользователь вручную снял exit node. Пока сеть не
     * сменилась, правило не включает ноду обратно.
     */
    public String getExitManualOffKey() {
        String value = prefs.getString(KEY_EXIT_MANUAL_OFF_KEY, null);
        return value == null ? "" : value;
    }

    public void setExitManualOffKey(String key) {
        String value = key == null ? "" : key.trim();
        if (value.length() == 0) {
            prefs.edit().remove(KEY_EXIT_MANUAL_OFF_KEY).apply();
        } else {
            prefs.edit().putString(KEY_EXIT_MANUAL_OFF_KEY, value).apply();
        }
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

    /**
     * Нейтральный статус: меняет только текст и время, не трогая прошлый
     * результат (last_ok) и прошлую ошибку (last_error).
     */
    public void recordNeutral(String status) {
        prefs.edit()
                .putString(KEY_LAST_STATUS, status == null ? "" : status)
                .putLong(KEY_LAST_TIME, System.currentTimeMillis())
                .apply();
    }
}
