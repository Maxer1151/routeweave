package com.vpnswitch;

import android.content.Context;
import android.content.Intent;
import android.net.Network;
import android.os.SystemClock;
import android.util.Log;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Управление exit node Tailscale: правило для Wi-Fi, watchdog при потере
 * интернета и отложенное восстановление.
 *
 * Tailscale остаётся подключённым: меняется только выбор exit node через
 * broadcast com.tailscale.ipn.USE_EXIT_NODE с extras "exitNode" и "allowLanAccess".
 * Все команды и сетевые проверки выполняются в фоновом потоке.
 */
public final class ExitNodeController {

    private static final String TAG = "VpnSwitcher";

    public static final String ACTION_USE_EXIT_NODE = "com.tailscale.ipn.USE_EXIT_NODE";

    public static final String EXTRA_EXIT_NODE = "exitNode";
    public static final String EXTRA_ALLOW_LAN_ACCESS = "allowLanAccess";

    /** Пауза перед повторной проверкой после снятия exit node watchdog'ом. */
    private static final long RETRY_DELAY_MS = 5L * 60L * 1000L;
    /** Сколько секунд после включения не проверять интернет: Tailscale ещё перестраивает путь. */
    private static final long WATCHDOG_GRACE_MS = 90L * 1000L;
    private static final int WATCHDOG_FAILS_TO_TRIP = 2;
    private static final int PROBE_TIMEOUT_MS = 3000;
    private static final int EXIT_PROBE_PORT_ALT = 41641;
    private static final int EXIT_PROBE_PORT_SSH = 22;
    private static final int EXIT_PROBE_PORT_HTTPS = 443;
    private static final int EXIT_PROBE_PORT_HTTP = 80;

    private static final ExecutorService EXECUTOR = Executors.newSingleThreadExecutor();

    private static volatile long lastOnCommandAt = -1L;
    /** Старт процесса: после перезагрузки Tailscale поднимает туннель не мгновенно. */
    private static final long PROCESS_STARTED_AT = SystemClock.elapsedRealtime();

    private ExitNodeController() {
    }

    // ------------------------------------------------------------------
    // Публичный API
    // ------------------------------------------------------------------

    /** Периодическая проверка: watchdog и восстановление после него. */
    public static void tickAsync(Context context) {
        if (context == null) {
            return;
        }
        final Context app = context.getApplicationContext();
        try {
            EXECUTOR.execute(new Runnable() {
                @Override
                public void run() {
                    try {
                        tick(app);
                    } catch (Throwable t) {
                        Log.e(TAG, "exitnode: tick crashed", t);
                    }
                }
            });
        } catch (Throwable t) {
            Log.e(TAG, "exitnode: tickAsync не удалось поставить задачу", t);
        }
    }

    /** Ручное включение/выключение exit node (кнопки, watchdog). */
    public static void applyNowAsync(Context context, final boolean on) {
        if (context == null) {
            return;
        }
        final Context app = context.getApplicationContext();
        try {
            EXECUTOR.execute(new Runnable() {
                @Override
                public void run() {
                    try {
                        Prefs prefs = new Prefs(app);
                        Log.i(TAG, "exitnode: applyNow on=" + on
                                + ", tailscale=" + isTailscaleActive(app)
                                + ", exitActive=" + isExitNodeActive(app));
                        if (on) {
                            applyOn(app, prefs, "команда");
                        } else {
                            applyOff(app, prefs, null, false);
                        }
                    } catch (Throwable t) {
                        Log.e(TAG, "exitnode: applyNow crashed", t);
                    }
                }
            });
        } catch (Throwable t) {
            Log.e(TAG, "exitnode: applyNowAsync не удалось поставить задачу", t);
        }
    }

    /**
     * Правило exit node для Wi-Fi: включает exit node на незнакомой Wi-Fi,
     * если он ещё не активен.
     */
    public static void ensureForWifi(final Context context) {
        if (context == null) {
            return;
        }
        final Context app = context.getApplicationContext();
        try {
            EXECUTOR.execute(new Runnable() {
                @Override
                public void run() {
                    try {
                        Prefs prefs = new Prefs(app);
                        if (!prefs.isExitManageEnabled()) {
                            Log.i(TAG, "exitnode: управление выключено, правило Wi-Fi пропущено");
                            return;
                        }
                        if (isExitNodeActive(app)) {
                            Log.i(TAG, "exitnode: exit node уже активен (правило Wi-Fi)");
                            return;
                        }
                        applyOn(app, prefs, "правило Wi-Fi");
                    } catch (Throwable t) {
                        Log.e(TAG, "exitnode: ensureForWifi crashed", t);
                    }
                }
            });
        } catch (Throwable t) {
            Log.e(TAG, "exitnode: ensureForWifi не удалось поставить задачу", t);
        }
    }

    /**
     * Exit node реально применён: Tailscale активен и в VPN-сети есть default route.
     * Без exit node Tailscale объявляет только 100.64.0.0/10, поэтому маршрута
     * по умолчанию в его LinkProperties нет.
     */
    public static boolean isExitNodeActive(Context context) {
        if (context == null) {
            return false;
        }
        VpnState vpn = NetworkUtils.detectVpn(context);
        if (!vpn.active || vpn.type != VpnType.TAILSCALE) {
            return false;
        }
        return NetworkUtils.hasDefaultRoute(context);
    }

    /** Строка состояния для карточки «Exit node». */
    public static String stateLine(Context context) {
        if (context == null) {
            return "";
        }
        Prefs prefs = new Prefs(context);
        if (!prefs.isExitManageEnabled()) {
            return context.getString(R.string.exit_state_disabled);
        }
        if (!NetworkUtils.isInstalled(context, NetworkUtils.PKG_TAILSCALE)) {
            return context.getString(R.string.exit_state_no_tailscale);
        }

        VpnState vpn = NetworkUtils.detectVpn(context);
        boolean tailscaleActive = vpn.active && vpn.type == VpnType.TAILSCALE;
        boolean exitActive = tailscaleActive && NetworkUtils.hasDefaultRoute(context);

        StringBuilder sb = new StringBuilder();
        if (exitActive) {
            sb.append(context.getString(R.string.exit_state_on, prefs.getExitNodeName()));
        } else if (tailscaleActive) {
            sb.append(context.getString(R.string.exit_state_off));
        } else {
            sb.append(context.getString(R.string.exit_state_no_vpn));
        }

        long retryAt = prefs.getExitRetryAt();
        if (retryAt > 0L) {
            long left = retryAt - System.currentTimeMillis();
            if (left > 0L) {
                long minutes = (left + 59999L) / 60000L;
                sb.append(" • ");
                sb.append(context.getString(R.string.exit_state_retry, minutes));
            }
        }

        if (!exitActive && prefs.getExitNodePendingOn()) {
            sb.append(" • ");
            sb.append(context.getString(R.string.exit_state_pending));
        } else if (!exitActive && tailscaleActive && prefs.isExitNodeApplied()) {
            sb.append(" • ");
            sb.append(context.getString(R.string.exit_state_unconfirmed));
        }
        return sb.toString();
    }

    // ------------------------------------------------------------------
    // Внутренняя логика (всегда в фоновом потоке)
    // ------------------------------------------------------------------

    private static void tick(Context context) {
        Prefs prefs = new Prefs(context);
        if (!prefs.isExitManageEnabled()) {
            return;
        }

        long nowMs = System.currentTimeMillis();
        long nowRealtime = SystemClock.elapsedRealtime();
        long retryAt = prefs.getExitRetryAt();
        boolean exitActive = isExitNodeActive(context);

        // 1. Watchdog: нет интернета через активную сеть (то есть через exit node).
        // Пока назначен retry, повторно не снимаем — иначе уведомления каждую минуту.
        boolean inGrace = nowRealtime - PROCESS_STARTED_AT < WATCHDOG_GRACE_MS
                || (lastOnCommandAt >= 0L
                && nowRealtime - lastOnCommandAt < WATCHDOG_GRACE_MS);
        if (prefs.isExitWatchdogEnabled() && exitActive && retryAt == 0L && !inGrace
                && NetworkUtils.currentKind(context) != NetKind.NONE) {
            boolean online = NetworkUtils.probeInternet(context);
            if (online) {
                if (prefs.getExitFailCount() != 0) {
                    prefs.setExitFailCount(0);
                    Log.i(TAG, "exitnode: интернет через exit node восстановился");
                }
            } else {
                int fails = prefs.getExitFailCount() + 1;
                prefs.setExitFailCount(fails);
                Log.i(TAG, "exitnode: watchdog, интернета нет (" + fails + "/"
                        + WATCHDOG_FAILS_TO_TRIP + ")");
                if (fails >= WATCHDOG_FAILS_TO_TRIP) {
                    applyOff(context, prefs, "нет интернета", true);
                    prefs.setExitFailCount(0);
                    prefs.setExitRetryAt(System.currentTimeMillis() + RETRY_DELAY_MS);
                    Log.i(TAG, "exitnode: exit node снят, повтор через 5 минут");
                }
            }
        }

        // 2. Восстановление после watchdog.
        if (retryAt != 0L && nowMs >= retryAt) {
            if (exitActive) {
                // Команда снятия не применилась или exit node включили вручную.
                if (NetworkUtils.probeInternet(context)) {
                    prefs.setExitRetryAt(0L);
                    prefs.setExitFailCount(0);
                    Log.i(TAG, "exitnode: интернет есть, отменяю повтор");
                } else {
                    Log.i(TAG, "exitnode: интернета всё ещё нет, повторяю снятие");
                    applyOff(context, prefs, "нет интернета", true);
                    prefs.setExitRetryAt(System.currentTimeMillis() + RETRY_DELAY_MS);
                }
            } else if (probeExitServer(context, prefs)) {
                prefs.setExitRetryAt(0L);
                applyOn(context, prefs, "восстановление после watchdog");
            } else {
                prefs.setExitRetryAt(System.currentTimeMillis() + RETRY_DELAY_MS);
                Log.i(TAG, "exitnode: сервер недоступен, повтор через 5 минут");
            }
        }
    }

    private static void applyOn(Context context, Prefs prefs, String reason) {
        String name = prefs.getExitNodeName();
        if (name == null) {
            name = "";
        }
        name = name.trim();
        if (name.length() == 0) {
            prefs.setExitNodePendingOn(false);
            String message = "Exit node: не задано имя ноды";
            Log.i(TAG, "exitnode: " + message + " (" + reason + ")");
            prefs.record(message, false, message);
            Notifications.updateMonitor(context);
            Notifications.showError(context, message, NetworkUtils.PKG_TAILSCALE);
            return;
        }

        if (!isTailscaleActive(context)) {
            // Tailscale должен быть подключён, иначе его worker не применит команду.
            prefs.setExitNodePendingOn(true);
            String status = "Exit node " + name + ": ожидание подключения Tailscale";
            Log.i(TAG, "exitnode: " + status + " (" + reason + ")");
            prefs.record(status, true, null);
            Notifications.updateMonitor(context);
            return;
        }

        sendExitNode(context, name, prefs.isExitNodeAllowLan());
        lastOnCommandAt = SystemClock.elapsedRealtime();
        prefs.setExitNodeApplied(true);
        prefs.setExitNodePendingOn(false);
        prefs.setExitRetryAt(0L);
        prefs.setExitFailCount(0);
        String status = "Exit node включён: " + name;
        Log.i(TAG, "exitnode: " + status + " (" + reason + ")");
        prefs.record(status, true, null);
        Notifications.clearError(context);
        Notifications.updateMonitor(context);
    }

    private static void applyOff(Context context, Prefs prefs, String reason, boolean error) {
        boolean tailscaleActive = isTailscaleActive(context);
        if (tailscaleActive) {
            sendExitNode(context, "", prefs.isExitNodeAllowLan());
        } else {
            Log.i(TAG, "exitnode: Tailscale не подключён, broadcast снятия не нужен");
        }

        prefs.setExitNodeApplied(false);
        prefs.setExitNodePendingOn(false);
        prefs.setExitRetryAt(0L);
        prefs.setExitFailCount(0);

        String status = reason == null || reason.length() == 0
                ? "Exit node снят"
                : "Exit node снят: " + reason;
        Log.i(TAG, "exitnode: " + status);
        prefs.record(status, !error, error ? status : null);
        Notifications.updateMonitor(context);
        if (error) {
            Notifications.showError(context, status, NetworkUtils.PKG_TAILSCALE);
        }
    }

    /** TCP-проба сервера exit node: сначала через Tailscale, затем через активную сеть. */
    private static boolean probeExitServer(Context context, Prefs prefs) {
        String host = prefs.getExitNodeIp();
        if (host == null) {
            host = "";
        }
        host = host.trim();
        if (host.length() == 0) {
            Log.i(TAG, "exitnode: не задан IP сервера для проверки");
            return false;
        }

        Network vpn = isTailscaleActive(context)
                ? NetworkUtils.findVpnNetwork(context)
                : null;
        Network active = NetworkUtils.activeNetwork(context);
        Network[] networks = vpn == null
                ? new Network[]{active}
                : new Network[]{vpn, active};

        int[] ports = new int[]{
                EXIT_PROBE_PORT_ALT, EXIT_PROBE_PORT_SSH,
                EXIT_PROBE_PORT_HTTPS, EXIT_PROBE_PORT_HTTP
        };
        for (int port : ports) {
            for (Network network : networks) {
                if (network == null) {
                    continue;
                }
                Log.d(TAG, "exitnode: проверяю " + host + ":" + port);
                if (NetworkUtils.probeTcp(network, host, port, PROBE_TIMEOUT_MS)) {
                    Log.i(TAG, "exitnode: сервер доступен " + host + ":" + port);
                    return true;
                }
            }
        }
        Log.i(TAG, "exitnode: сервер " + host + " недоступен");
        return false;
    }

    private static void sendExitNode(Context context, String exitNode, boolean allowLanAccess) {
        try {
            Intent intent = new Intent(ACTION_USE_EXIT_NODE);
            intent.setPackage(NetworkUtils.PKG_TAILSCALE);
            intent.putExtra(EXTRA_EXIT_NODE, exitNode == null ? "" : exitNode);
            intent.putExtra(EXTRA_ALLOW_LAN_ACCESS, allowLanAccess);
            context.sendBroadcast(intent);
            Log.i(TAG, "exitnode: broadcast " + ACTION_USE_EXIT_NODE
                    + " exitNode=\"" + (exitNode == null ? "" : exitNode) + "\""
                    + " allowLanAccess=" + allowLanAccess);
        } catch (Throwable t) {
            Log.e(TAG, "exitnode: broadcast " + ACTION_USE_EXIT_NODE + " failed", t);
        }
    }

    private static boolean isTailscaleActive(Context context) {
        VpnState vpn = NetworkUtils.detectVpn(context);
        return vpn.active && vpn.type == VpnType.TAILSCALE;
    }
}
