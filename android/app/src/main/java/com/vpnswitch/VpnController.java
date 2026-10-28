package com.vpnswitch;

import android.content.Context;
import android.content.Intent;
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
     * Досрочно отключает Tailscale, если он активен. Выполняется в общей
     * очереди с apply: если применение уже идёт, не вмешиваемся — следующий
     * apply сам решит судьбу Tailscale по текущей сети. Иначе broadcast
     * отключения мог бы отменить параллельное подключение.
     */
    public static void disconnectTailscale(Context context) {
        if (context == null) {
            return;
        }
        final Context app = context.getApplicationContext();
        if (!APPLYING.compareAndSet(false, true)) {
            Log.i(TAG, "disconnectTailscale: apply уже выполняется, пропуск");
            return;
        }
        try {
            EXECUTOR.execute(new Runnable() {
                @Override
                public void run() {
                    try {
                        disconnectTailscaleNow(app);
                    } catch (Throwable t) {
                        Log.e(TAG, "disconnectTailscale crashed", t);
                    } finally {
                        APPLYING.set(false);
                    }
                }
            });
        } catch (Throwable t) {
            APPLYING.set(false);
            Log.e(TAG, "disconnectTailscale: не удалось поставить задачу", t);
        }
    }

    /** Само отключение: только в фоновом потоке, под флагом APPLYING. */
    private static void disconnectTailscaleNow(Context app) {
        VpnState state = NetworkUtils.detectVpn(app);
        if (!state.active || state.type != VpnType.TAILSCALE) {
            Log.i(TAG, "disconnectTailscale: Tailscale не активен, команда не отправлена");
            return;
        }
        send(app, ACTION_TS_DISCONNECT, NetworkUtils.PKG_TAILSCALE);
        Log.i(TAG, "disconnectTailscale: Tailscale отключён досрочно");
    }

    /** Ручное включение/выключение Tailscale в общей очереди с applyAsync. */
    public static boolean manualTailscale(Context context, boolean connect) {
        return manualVpn(context, VpnType.TAILSCALE, connect);
    }

    /** Ручное включение/выключение WDTT Plus в общей очереди с applyAsync. */
    public static boolean manualWdtt(Context context, boolean connect) {
        return manualVpn(context, VpnType.WDTT, connect);
    }

    /**
     * Общая очередь ручных действий: тот же EXECUTOR и флаг APPLYING, что у
     * applyAsync. false — контроллер занят, команда не отправлена.
     */
    private static boolean manualVpn(final Context context, final VpnType type,
                                     final boolean connect) {
        if (context == null) {
            return false;
        }
        if (!APPLYING.compareAndSet(false, true)) {
            Log.i(TAG, "manualVpn пропущен (уже выполняется): type=" + type
                    + ", connect=" + connect);
            return false;
        }
        final Context app = context.getApplicationContext();
        try {
            EXECUTOR.execute(new Runnable() {
                @Override
                public void run() {
                    try {
                        runManual(app, type, connect);
                    } catch (Throwable t) {
                        Log.e(TAG, "manualVpn crashed: type=" + type, t);
                    } finally {
                        APPLYING.set(false);
                        Notifications.updateMonitor(app);
                    }
                }
            });
            return true;
        } catch (Throwable t) {
            APPLYING.set(false);
            Log.e(TAG, "manualVpn: не удалось поставить задачу", t);
            return false;
        }
    }

    private static void runManual(Context app, final VpnType type, boolean connect) {
        VpnState state = NetworkUtils.detectVpn(app);
        Log.i(TAG, "manualVpn: type=" + type + ", connect=" + connect
                + ", state=" + state.type);
        if (connect) {
            if (state.active) {
                Log.i(TAG, "manualVpn: VPN уже активен, команда не отправлена");
                return;
            }
            start(app, type);
            waitUntil(new Condition() {
                @Override
                public boolean isTrue() {
                    VpnState current = NetworkUtils.detectVpn(app);
                    return current.active && current.type == type;
                }
            }, 15000L);
            VpnState after = NetworkUtils.detectVpn(app);
            if (type == VpnType.WDTT && after.active && after.type == VpnType.WDTT) {
                // Пауза снимается только по подтверждённому WDTT (см. D8).
                new Prefs(app).setWdttPausedOnWifi(false);
            }
            if (type == VpnType.TAILSCALE) {
                // Подключение могло снять pending exit node или включить правило.
                ExitNodeController.tickAsync(app);
            }
            return;
        }

        if (!state.active || state.type != type) {
            Log.i(TAG, "manualVpn: VPN не активен, команда не отправлена");
            return;
        }
        stop(app, type);
        waitUntil(new Condition() {
            @Override
            public boolean isTrue() {
                return NetworkUtils.detectVpn(app).type != type;
            }
        }, 8000L);
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
            // Ethernet/нет сети — не успех и не ошибка: прошлый результат не трогаем.
            String status = "Нет сети — ожидание";
            Log.i(TAG, "apply: " + status + " (" + NetworkUtils.kindLabel(kind) + ")");
            prefs.recordNeutral(status);
            if (prefs.isExitManageEnabled()) {
                ExitNodeController.onNetworkChangedAsync(context);
            }
            Notifications.updateMonitor(context);
            return;
        }

        // Wi-Fi: доверенная пара SSID+BSSID выключает VPN, незнакомая — Tailscale.
        // Мобильная сеть: WDTT Plus. UNBOUND/MISMATCH обрабатываются как
        // незнакомая Wi-Fi: VPN не отключается, нужен Tailscale + exit node.
        NetworkUtils.WifiAccess access = kind == NetKind.WIFI
                ? NetworkUtils.currentWifiAccess(context) : null;
        if (kind == NetKind.WIFI && (access == null || access.ssid == null)) {
            Log.i(TAG, "apply: SSID недоступен (" + NetworkUtils.ssidUnavailableReason(context)
                    + "), сеть считается незнакомой");
        }
        boolean trusted = access != null && access.trust == Logic.Trust.TRUSTED;
        if (kind == NetKind.WIFI) {
            Log.d(TAG, "apply: Wi-Fi trust="
                    + (access == null ? Logic.Trust.NONE : access.trust));
        }
        VpnType target = Logic.targetFor(kind, trusted);

        // Правило exit node не зависит от auto: на trusted/mobile его применяет
        // эта же сверка, незнакомую Wi-Fi обрабатывает ensureForWifi ниже.
        if (prefs.isExitManageEnabled() && target != VpnType.TAILSCALE) {
            ExitNodeController.onNetworkChangedAsync(context);
        }

        VpnState state = NetworkUtils.detectVpn(context);
        // WDTT работает, значит он не в паузе на доверенной Wi-Fi.
        if (state.active && state.type == VpnType.WDTT) {
            prefs.setWdttPausedOnWifi(false);
            if (kind == NetKind.WIFI && prefs.isWdttTrusted()) {
                // Запоминаем, что WDTT был активен на Wi-Fi: если он уйдёт в
                // авто-паузу раньше callback'а, уход всё равно будет распознан.
                prefs.setWdttActiveOnWifi(true);
            }
        }

        if (target != VpnType.NONE) {
            String targetPkg = packageOf(target);
            if (!NetworkUtils.isInstalled(context, targetPkg)) {
                fail(context, label(target) + " не установлен", targetPkg);
                return;
            }
        }

        if (target == VpnType.NONE) {
            applyTrustedWifi(context, prefs, state, access);
        } else if (target == VpnType.TAILSCALE) {
            applyUntrustedWifi(context, prefs, state, access);
        } else {
            applyMobile(context, prefs, state, manual);
        }
    }

    /** Доверенная Wi-Fi: любой активный VPN должен быть выключен. */
    private static void applyTrustedWifi(Context context, Prefs prefs, VpnState state,
                                         NetworkUtils.WifiAccess access) {
        Log.d(TAG, "apply: доверенная Wi-Fi, trust="
                + (access == null ? Logic.Trust.NONE : access.trust));
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

    /** Незнакомая Wi-Fi (или SSID/BSSID недоступен): Tailscale + exit node. */
    private static void applyUntrustedWifi(Context context, Prefs prefs, VpnState state,
                                           NetworkUtils.WifiAccess access) {
        Log.d(TAG, "apply: незнакомая Wi-Fi, trust="
                + (access == null ? Logic.Trust.NONE : access.trust));
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
        if (state.active && state.type == VpnType.OTHER) {
            fail(context, otherVpnMessage(state), state.ownerPackage);
            return;
        }
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
        // вслепую: всегда сначала ждём авто-восстановление 12 с, в том числе при
        // ручном запуске. Точная семантика паузы WDTT (когда именно клиент уходит
        // в паузу и как её снимает toggle) требует проверки на устройстве.
        boolean wdttTrusted = prefs.isWdttTrusted();
        boolean recentlyLeftWifi = prefs.hasLeftWifiRecently(120000L);
        boolean wasPaused = prefs.isWdttPausedOnWifi();
        if (wdttTrusted && recentlyLeftWifi) {
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
                if (state.type == VpnType.OTHER) {
                    fail(context, otherVpnMessage(state), state.ownerPackage);
                } else {
                    fail(context, "Другой VPN занял слот: " + NetworkUtils.vpnLabel(state),
                            state.ownerPackage);
                }
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

        record(context, "Подключаю WDTT Plus", true, null);
        start(context, VpnType.WDTT);

        boolean appeared = waitUntil(new Condition() {
            @Override
            public boolean isTrue() {
                return NetworkUtils.detectVpn(context).active;
            }
        }, 20000L);

        state = NetworkUtils.detectVpn(context);
        if (state.active && state.type == VpnType.OTHER) {
            // Чужой VPN не считается успехом WDTT: не очищаем ошибку и не
            // сбрасываем wdtt_paused_on_wifi.
            fail(context, otherVpnMessage(state), state.ownerPackage);
            return;
        }
        if (appeared && state.type == VpnType.WDTT) {
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
            // Флаг паузы сбрасывается только после подтверждённого WDTT,
            // а не до отправки toggle (см. D8).
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
            // Признак паузы здесь не сбрасываем: ручная остановка не доказывает,
            // что WDTT не был в паузе до неё (см. отслеживание в apply).
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
                prefs.setWdttActiveOnWifi(true);
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
        // Паузу не сбрасываем: подтверждённого активного WDTT не было, а
        // остановка вручную не доказывает, что паузы не было до неё.
        prefs.setWdttActiveOnWifi(false);
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
            Log.d(TAG, "broadcast: " + action + " -> " + pkg);
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
        String logMessage = targetPackage == null || message == null
                ? message
                : message.replace(targetPackage, Logic.maskHost(targetPackage));
        Log.d(TAG, "FAIL: " + logMessage);
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
}
