package com.vpnswitch;

import android.content.Context;
import android.content.Intent;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Вся логика переключения VPN.
 *
 * Чужой VpnService нельзя запустить напрямую, поэтому используются штатные
 * broadcast-точки управления: IPNReceiver у Tailscale и VpnWidgetProvider у WDTT Plus.
 */
public final class VpnController {

    private static final String TAG = "VpnSwitcher";

    private static final String ACTION_TS_CONNECT = "com.tailscale.ipn.CONNECT_VPN";
    private static final String ACTION_TS_DISCONNECT = "com.tailscale.ipn.DISCONNECT_VPN";
    private static final String ACTION_WDTT_TOGGLE = "com.wdtt.plus.ACTION_WIDGET_TOGGLE";

    private static final long POLL_INTERVAL_MS = 350L;

    private static final ExecutorService EXECUTOR = Executors.newSingleThreadExecutor();
    private static final AtomicBoolean APPLYING = new AtomicBoolean(false);
    private static final Handler MAIN = new Handler(Looper.getMainLooper());

    private interface Condition {
        boolean isTrue();
    }

    private VpnController() {
    }

    /** Запускает применение правила в фоне. Параллельные запуски игнорируются. */
    public static void applyAsync(final Context context, final String reason,
                                  final boolean manual) {
        if (context == null) {
            return;
        }
        if (!APPLYING.compareAndSet(false, true)) {
            Log.i(TAG, "apply пропущен (уже выполняется): " + reason);
            return;
        }
        final Context app = context.getApplicationContext();
        try {
            EXECUTOR.execute(new Runnable() {
                @Override
                public void run() {
                    try {
                        apply(app, reason, manual);
                    } catch (Throwable t) {
                        Log.e(TAG, "apply crashed", t);
                        fail(app, "Ошибка переключения: " + t.getClass().getSimpleName(), null);
                    } finally {
                        APPLYING.set(false);
                        Notifications.updateMonitor(app);
                    }
                }
            });
        } catch (Throwable t) {
            APPLYING.set(false);
            Log.e(TAG, "applyAsync: не удалось поставить задачу", t);
        }
    }

    public static boolean isApplying() {
        return APPLYING.get();
    }

    /**
     * Досрочно отключает Tailscale, если он активен. Команда только отправляется,
     * результат не ожидается и поток не блокируется.
     */
    public static void disconnectTailscale(Context context) {
        if (context == null) {
            return;
        }
        Context app = context.getApplicationContext();
        VpnState state = NetworkUtils.detectVpn(app);
        if (!state.active || state.type != VpnType.TAILSCALE) {
            Log.i(TAG, "disconnectTailscale: Tailscale не активен, команда не отправлена");
            return;
        }
        send(app, ACTION_TS_DISCONNECT, NetworkUtils.PKG_TAILSCALE);
        Log.i(TAG, "disconnectTailscale: Tailscale отключён досрочно");
    }

    /** Ручное включение/выключение Tailscale без ожидания результата. */
    public static void manualTailscale(Context context, boolean connect) {
        if (context == null) {
            return;
        }
        Context app = context.getApplicationContext();
        VpnState state = NetworkUtils.detectVpn(app);
        Log.i(TAG, "manualTailscale: connect=" + connect + ", state=" + state.type);
        if (connect) {
            if (state.active) {
                Log.i(TAG, "manualTailscale: VPN уже активен, команда не отправлена");
                return;
            }
            start(app, VpnType.TAILSCALE);
        } else {
            stop(app, VpnType.TAILSCALE);
        }
        refreshSoon(app);
    }

    /** Ручное включение/выключение WDTT Plus без ожидания результата. */
    public static void manualWdtt(Context context, boolean connect) {
        if (context == null) {
            return;
        }
        Context app = context.getApplicationContext();
        VpnState state = NetworkUtils.detectVpn(app);
        Log.i(TAG, "manualWdtt: connect=" + connect + ", state=" + state.type);
        if (connect) {
            if (state.active) {
                Log.i(TAG, "manualWdtt: VPN уже активен, toggle не отправлен");
                return;
            }
            start(app, VpnType.WDTT);
        } else {
            stop(app, VpnType.WDTT);
        }
        refreshSoon(app);
    }

    public static void openApp(Context context, String pkg) {
        if (context == null || pkg == null) {
            return;
        }
        try {
            Intent launch = context.getPackageManager().getLaunchIntentForPackage(pkg);
            if (launch == null) {
                Log.i(TAG, "openApp: нет launch intent для " + pkg);
                return;
            }
            launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            context.startActivity(launch);
        } catch (Throwable t) {
            Log.e(TAG, "openApp failed: " + pkg, t);
        }
    }

    // ---------------------------------------------------------------------

    private static void apply(Context context, String reason, boolean manual) {
        Prefs prefs = new Prefs(context);
        Log.i(TAG, "apply: reason=" + reason + ", manual=" + manual
                + ", auto=" + prefs.isAutoEnabled());

        if (!manual && !prefs.isAutoEnabled()) {
            Log.i(TAG, "apply: автопереключение выключено, пропуск");
            return;
        }

        NetKind kind = NetworkUtils.currentKind(context);
        if (kind == NetKind.OTHER || kind == NetKind.NONE) {
            record(context, "Нет сети — ожидание", true, null);
            return;
        }

        // Wi-Fi: доверенный SSID выключает VPN, незнакомый — Tailscale.
        // Мобильная сеть: WDTT Plus.
        String ssid = kind == NetKind.WIFI ? NetworkUtils.currentSsid(context) : null;
        if (kind == NetKind.WIFI && ssid == null) {
            Log.i(TAG, "apply: SSID недоступен (" + NetworkUtils.ssidUnavailableReason(context)
                    + "), сеть считается незнакомой");
        }
        boolean trusted = ssid != null && prefs.isTrustedSsid(ssid);
        VpnType target;
        if (kind == NetKind.WIFI) {
            target = trusted ? VpnType.NONE : VpnType.TAILSCALE;
        } else {
            target = VpnType.WDTT;
        }

        VpnState state = NetworkUtils.detectVpn(context);
        // WDTT работает, значит он не в паузе на доверенной Wi-Fi.
        if (state.active && state.type == VpnType.WDTT) {
            prefs.setWdttPausedOnWifi(false);
        }

        if (target != VpnType.NONE) {
            String targetPkg = packageOf(target);
            if (!NetworkUtils.isInstalled(context, targetPkg)) {
                fail(context, label(target) + " не установлен", targetPkg);
                return;
            }
        }

        if (target == VpnType.NONE) {
            applyTrustedWifi(context, prefs, state);
        } else if (target == VpnType.TAILSCALE) {
            applyUntrustedWifi(context, prefs, state);
        } else {
            applyMobile(context, prefs, state, manual);
        }
    }

    /** Доверенная Wi-Fi: любой активный VPN должен быть выключен. */
    private static void applyTrustedWifi(Context context, Prefs prefs, VpnState state) {
        if (state.active && state.type == VpnType.OTHER) {
            fail(context, otherVpnMessage(state), state.ownerPackage);
            return;
        }
        if (state.active && state.type == VpnType.TAILSCALE) {
            record(context, "Отключаю Tailscale", true, null);
            stop(context, VpnType.TAILSCALE);
            boolean stopped = waitUntil(new Condition() {
                @Override
                public boolean isTrue() {
                    return NetworkUtils.detectVpn(context).type != VpnType.TAILSCALE;
                }
            }, 8000L);
            if (!stopped) {
                fail(context, "Не удалось отключить Tailscale", NetworkUtils.PKG_TAILSCALE);
                return;
            }
        } else if (state.active && state.type == VpnType.WDTT) {
            record(context, "Отключаю WDTT Plus", true, null);
            if (!stopWdttForSwitch(context, prefs)) {
                fail(context, "Не удалось отключить WDTT Plus", NetworkUtils.PKG_WDTT);
                return;
            }
        }

        state = NetworkUtils.detectVpn(context);
        if (state.active) {
            if (state.type == VpnType.OTHER) {
                fail(context, otherVpnMessage(state), state.ownerPackage);
            } else {
                fail(context, "Не удалось отключить " + label(state.type),
                        packageOf(state.type));
            }
            return;
        }
        record(context, "Доверенная Wi-Fi • VPN выключен", true, null);
    }

    /** Незнакомая Wi-Fi (или SSID недоступен): Tailscale + exit node. */
    private static void applyUntrustedWifi(Context context, Prefs prefs, VpnState state) {
        if (state.active && state.type == VpnType.OTHER) {
            fail(context, otherVpnMessage(state), state.ownerPackage);
            return;
        }
        if (state.active && state.type == VpnType.TAILSCALE) {
            record(context, "Wi-Fi • Tailscale уже подключён", true, null);
            ExitNodeController.ensureForWifi(context);
            return;
        }
        if (state.active && state.type == VpnType.WDTT) {
            record(context, "Отключаю WDTT Plus", true, null);
            if (!stopWdttForSwitch(context, prefs)) {
                fail(context, "Не удалось отключить WDTT Plus", NetworkUtils.PKG_WDTT);
                return;
            }
            state = NetworkUtils.detectVpn(context);
            if (state.active) {
                if (state.type == VpnType.OTHER) {
                    fail(context, otherVpnMessage(state), state.ownerPackage);
                } else {
                    fail(context, "Не удалось отключить " + label(state.type),
                            packageOf(state.type));
                }
                return;
            }
        }

        record(context, "Подключаю Tailscale", true, null);
        start(context, VpnType.TAILSCALE);

        boolean appeared = waitUntil(new Condition() {
            @Override
            public boolean isTrue() {
                return NetworkUtils.detectVpn(context).active;
            }
        }, 15000L);

        // Tailscale иногда поднимается с задержкой — одна повторная попытка.
        if (!appeared) {
            Log.i(TAG, "apply: Tailscale не появился, повторная попытка");
            start(context, VpnType.TAILSCALE);
            appeared = waitUntil(new Condition() {
                @Override
                public boolean isTrue() {
                    return NetworkUtils.detectVpn(context).active;
                }
            }, 12000L);
        }

        state = NetworkUtils.detectVpn(context);
        if (appeared && state.type == VpnType.TAILSCALE) {
            record(context, "Wi-Fi • Tailscale подключён", true, null);
            ExitNodeController.ensureForWifi(context);
            return;
        }
        fail(context, "Tailscale не подключился. Проверьте VPN-разрешение"
                + " и вход в аккаунт.", NetworkUtils.PKG_TAILSCALE);
    }

    /** Мобильная сеть: WDTT Plus, при необходимости с ожиданием авто-восстановления. */
    private static void applyMobile(Context context, Prefs prefs, VpnState state,
                                    boolean manual) {
        if (state.active && state.type == VpnType.WDTT) {
            prefs.setWdttPausedOnWifi(false);
            record(context, "Моб. сеть • WDTT Plus уже подключён", true, null);
            return;
        }
        if (state.active && state.type == VpnType.OTHER) {
            fail(context, otherVpnMessage(state), state.ownerPackage);
            return;
        }
        if (state.active && state.type == VpnType.TAILSCALE) {
            record(context, "Отключаю Tailscale", true, null);
            stop(context, VpnType.TAILSCALE);
            boolean stopped = waitUntil(new Condition() {
                @Override
                public boolean isTrue() {
                    return NetworkUtils.detectVpn(context).type != VpnType.TAILSCALE;
                }
            }, 8000L);
            if (!stopped) {
                fail(context, "Не удалось отключить Tailscale", NetworkUtils.PKG_TAILSCALE);
                return;
            }
            state = NetworkUtils.detectVpn(context);
            if (state.active) {
                if (state.type == VpnType.OTHER) {
                    fail(context, otherVpnMessage(state), state.ownerPackage);
                } else {
                    fail(context, "Другой VPN занял слот: " + NetworkUtils.vpnLabel(state),
                            state.ownerPackage);
                }
                return;
            }
        }

        // После выхода из доверенной Wi-Fi WDTT восстанавливается сам. Не шлём toggle
        // вслепую: сначала ждём авто-восстановление, иначе можно его выключить.
        // Но если WDTT не был в паузе (холодный старт), toggle как раз и нужен.
        boolean wdttTrusted = prefs.isWdttTrusted();
        boolean recentlyLeftWifi = prefs.hasLeftWifiRecently(120000L);
        if (wdttTrusted && recentlyLeftWifi) {
            boolean wasPaused = prefs.isWdttPausedOnWifi();
            boolean skipWait = manual && !wasPaused;

            if (skipWait) {
                Log.i(TAG, "apply: WDTT не был на паузе, ручной запуск без ожидания");
            } else {
                record(context, "Ожидаю авто-восстановление WDTT Plus…", true, null);
                if (waitUntil(new Condition() {
                    @Override
                    public boolean isTrue() {
                        return NetworkUtils.detectVpn(context).type == VpnType.WDTT;
                    }
                }, 12000L)) {
                    prefs.setWdttPausedOnWifi(false);
                    record(context, "WDTT Plus восстановился автоматически", true, null);
                    return;
                }

                state = NetworkUtils.detectVpn(context);
                if (state.type == VpnType.WDTT) {
                    prefs.setWdttPausedOnWifi(false);
                    record(context, "WDTT Plus восстановился автоматически", true, null);
                    return;
                }
                if (state.active) {
                    fail(context, "Другой VPN занял слот: " + NetworkUtils.vpnLabel(state),
                            state.ownerPackage);
                    return;
                }
                if (!manual && wasPaused) {
                    fail(context, "WDTT Plus не восстановился автоматически после выхода из Wi-Fi."
                                    + " Откройте WDTT Plus и включите VPN вручную или проверьте"
                                    + " «Доверенные сети».",
                            NetworkUtils.PKG_WDTT);
                    return;
                }
                if (!manual) {
                    record(context, "WDTT Plus не восстановился автоматически — запускаю принудительно",
                            true, null);
                } else {
                    Log.i(TAG, "apply: ручной режим, после ожидания отправляю toggle WDTT");
                }
            }
        }

        record(context, "Подключаю WDTT Plus", true, null);
        start(context, VpnType.WDTT);

        boolean appeared = waitUntil(new Condition() {
            @Override
            public boolean isTrue() {
                return NetworkUtils.detectVpn(context).active;
            }
        }, 20000L);

        state = NetworkUtils.detectVpn(context);
        if (appeared && (state.type == VpnType.WDTT || state.type == VpnType.OTHER)) {
            prefs.setWdttPausedOnWifi(false);
            record(context, "Моб. сеть • WDTT Plus подключён", true, null);
            return;
        }
        fail(context, "WDTT Plus не запустился. Проверьте VPN-разрешение"
                + " и «Доверенные сети» в WDTT.", NetworkUtils.PKG_WDTT);
    }

    private static void start(Context context, VpnType type) {
        if (type == VpnType.TAILSCALE) {
            send(context, ACTION_TS_CONNECT, NetworkUtils.PKG_TAILSCALE);
        } else if (type == VpnType.WDTT) {
            if (NetworkUtils.detectVpn(context).active) {
                Log.i(TAG, "start WDTT: VPN уже активен, toggle не отправлен");
                return;
            }
            // Toggle запускает WDTT, то есть паузы больше нет.
            new Prefs(context).setWdttPausedOnWifi(false);
            send(context, ACTION_WDTT_TOGGLE, NetworkUtils.PKG_WDTT);
        }
    }

    private static boolean stop(Context context, VpnType type) {
        if (type == VpnType.TAILSCALE) {
            VpnState state = NetworkUtils.detectVpn(context);
            if (!state.active || state.type != VpnType.TAILSCALE) {
                Log.i(TAG, "stop Tailscale: Tailscale не активен, команда не отправлена");
                return false;
            }
            send(context, ACTION_TS_DISCONNECT, NetworkUtils.PKG_TAILSCALE);
            return true;
        }
        if (type == VpnType.WDTT) {
            VpnState state = NetworkUtils.detectVpn(context);
            if (!state.active || state.type != VpnType.WDTT) {
                Log.i(TAG, "stop WDTT: WDTT не активен, toggle не отправлен");
                return false;
            }
            // WDTT остановлен вручную — значит, он не в паузе на доверенной Wi-Fi.
            new Prefs(context).setWdttPausedOnWifi(false);
            send(context, ACTION_WDTT_TOGGLE, NetworkUtils.PKG_WDTT);
            return true;
        }
        return false;
    }

    /**
     * Останавливает WDTT Plus перед переключением. Если WDTT сам уходит в паузу
     * на своей доверенной Wi-Fi, сначала ждём авто-паузу; иначе отправляем toggle.
     */
    private static boolean stopWdttForSwitch(Context context, Prefs prefs) {
        VpnState state = NetworkUtils.detectVpn(context);
        if (!state.active || state.type != VpnType.WDTT) {
            return true;
        }

        if (prefs.isWdttTrusted()) {
            Log.i(TAG, "stopWdttForSwitch: ждём авто-паузу WDTT");
            waitUntil(new Condition() {
                @Override
                public boolean isTrue() {
                    return NetworkUtils.detectVpn(context).type != VpnType.WDTT;
                }
            }, 5000L);
            state = NetworkUtils.detectVpn(context);
            if (state.type != VpnType.WDTT) {
                prefs.setWdttPausedOnWifi(true);
                Log.i(TAG, "stopWdttForSwitch: WDTT ушёл в паузу");
                return true;
            }
        }

        if (!stop(context, VpnType.WDTT)) {
            Log.i(TAG, "stopWdttForSwitch: WDTT уже не активен");
            return true;
        }
        boolean stopped = waitUntil(new Condition() {
            @Override
            public boolean isTrue() {
                return NetworkUtils.detectVpn(context).type != VpnType.WDTT;
            }
        }, 8000L);
        if (!stopped) {
            Log.i(TAG, "stopWdttForSwitch: WDTT не отключился");
            return false;
        }
        prefs.setWdttPausedOnWifi(false);
        return true;
    }

    private static String otherVpnMessage(VpnState state) {
        return "Активен другой VPN"
                + (state == null || state.ownerPackage == null
                ? "" : " (" + state.ownerPackage + ")")
                + ". Отключите его вручную.";
    }

    private static void send(Context context, String action, String pkg) {
        try {
            Intent intent = new Intent(action);
            intent.setPackage(pkg);
            context.sendBroadcast(intent);
            Log.i(TAG, "broadcast: " + action + " -> " + pkg);
        } catch (SecurityException e) {
            Log.e(TAG, "broadcast запрещён: " + action + " -> " + pkg, e);
        } catch (Throwable t) {
            Log.e(TAG, "broadcast failed: " + action + " -> " + pkg, t);
        }
    }

    private static boolean waitUntil(Condition condition, long timeoutMs) {
        long start = SystemClock.elapsedRealtime();
        while (SystemClock.elapsedRealtime() - start < timeoutMs) {
            try {
                if (condition.isTrue()) {
                    return true;
                }
            } catch (Throwable t) {
                Log.d(TAG, "waitUntil: проверка состояния: " + t);
            }
            try {
                Thread.sleep(POLL_INTERVAL_MS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return false;
            }
        }
        try {
            return condition.isTrue();
        } catch (Throwable t) {
            return false;
        }
    }

    private static void record(Context context, String status, boolean ok, String error) {
        new Prefs(context).record(status, ok, error);
        if (ok) {
            // Успешное действие снимает старое уведомление об ошибке.
            Notifications.clearError(context);
            Log.i(TAG, "OK: " + status);
        } else {
            Log.i(TAG, "FAIL: " + status);
        }
        Notifications.updateMonitor(context);
    }

    private static void fail(Context context, String message, String targetPackage) {
        new Prefs(context).record(message, false, message);
        Log.i(TAG, "FAIL: " + message);
        Notifications.updateMonitor(context);
        Notifications.showError(context, message, targetPackage);
    }

    private static String packageOf(VpnType type) {
        if (type == VpnType.TAILSCALE) {
            return NetworkUtils.PKG_TAILSCALE;
        }
        if (type == VpnType.WDTT) {
            return NetworkUtils.PKG_WDTT;
        }
        return null;
    }

    private static String label(VpnType type) {
        return NetworkUtils.vpnLabel(type);
    }

    private static void refreshSoon(Context context) {
        final Context app = context.getApplicationContext();
        MAIN.postDelayed(new Runnable() {
            @Override
            public void run() {
                Notifications.updateMonitor(app);
            }
        }, 2000L);
    }
}
