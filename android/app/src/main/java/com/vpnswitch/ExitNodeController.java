package com.vpnswitch;

import android.content.Context;
import android.content.Intent;
import android.content.pm.ResolveInfo;
import android.net.Network;
import android.os.SystemClock;
import android.util.Log;

import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * Управление exit node Tailscale: правило для Wi-Fi, watchdog при потере
 * интернета и отложенное восстановление.
 *
 * Tailscale остаётся подключённым: меняется только выбор exit node через
 * broadcast com.tailscale.ipn.USE_EXIT_NODE с extras "exitNode" и "allowLanAccess".
 * Все команды и сетевые проверки выполняются в фоновом потоке.
 *
 * Правило exit node не зависит от автопереключения: пока включено управление,
 * сервис периодически сверяет желаемое состояние с фактическим. Включение
 * подтверждается появлением default route в сети Tailscale, а не фактом отправки
 * broadcast.
 */
public final class ExitNodeController {

    private static final String TAG = "VpnSwitcher";

    public static final String ACTION_USE_EXIT_NODE = "com.tailscale.ipn.USE_EXIT_NODE";

    public static final String EXTRA_EXIT_NODE = "exitNode";
    public static final String EXTRA_ALLOW_LAN_ACCESS = "allowLanAccess";

    /** Сколько ждать подтверждения включения exit node после команды. */
    private static final long CONFIRM_TIMEOUT_MS = 12L * 1000L;
    /** Сколько секунд после включения не проверять интернет: Tailscale ещё перестраивает путь. */
    private static final long WATCHDOG_GRACE_MS = 90L * 1000L;
    private static final int WATCHDOG_FAILS_TO_TRIP = 2;
    /** После стольких неудачных попыток автовосстановление останавливается. */
    private static final int MAX_RETRY_ATTEMPTS = 5;
    private static final int PROBE_TIMEOUT_MS = 3000;
    private static final int EXIT_PROBE_PORT_ALT = 41641;
    private static final int EXIT_PROBE_PORT_SSH = 22;
    private static final int EXIT_PROBE_PORT_HTTPS = 443;
    private static final int EXIT_PROBE_PORT_HTTP = 80;

    private static final ExecutorService EXECUTOR = Executors.newSingleThreadExecutor();
    private static final ScheduledExecutorService SCHEDULER =
            Executors.newSingleThreadScheduledExecutor();

    private static volatile long lastOnCommandAt = -1L;
    /** Старт процесса: после перезагрузки Tailscale поднимает туннель не мгновенно. */
    private static final long PROCESS_STARTED_AT = SystemClock.elapsedRealtime();

    private ExitNodeController() {
    }

    // ------------------------------------------------------------------
    // Публичный API
    // ------------------------------------------------------------------

    /** Периодическая сверка: pending, подтверждение, правило сети, watchdog и retry. */
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

    /**
     * Правило exit node для текущей сети, независимо от автопереключения.
     * Незнакомая Wi-Fi — включить, доверенная — снять, мобильная/нет сети —
     * только сбросить pending/retry.
     */
    public static void onNetworkChangedAsync(Context context) {
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
                            return;
                        }
                        reconcile(app, prefs);
                    } catch (Throwable t) {
                        Log.e(TAG, "exitnode: onNetworkChanged crashed", t);
                    }
                }
            });
        } catch (Throwable t) {
            Log.e(TAG, "exitnode: onNetworkChangedAsync не удалось поставить задачу", t);
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
                            if (!prefs.isExitManageEnabled()) {
                                // Кнопка доступна только при включённом
                                // управлении, но проверка нужна и здесь: между
                                // нажатием и выполнением toggle могли выключить.
                                Log.i(TAG, "exitnode: ручное включение пропущено —"
                                        + " управление exit node выключено");
                                return;
                            }
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
     * Правило exit node для Wi-Fi. Оставлено как точка входа для VpnController:
     * фактическую логику выполняет общая сверка по текущей сети.
     */
    public static void ensureForWifi(Context context) {
        onNetworkChangedAsync(context);
    }

    /**
     * Явное выключение управления exit node. Сбрасывает pending/retry/счётчики
     * и, если exit node включало приложение и Tailscale активен, отправляет
     * команду снятия.
     */
    public static void disableManagementAsync(Context context) {
        if (context == null) {
            return;
        }
        final Context app = context.getApplicationContext();
        try {
            EXECUTOR.execute(new Runnable() {
                @Override
                public void run() {
                    try {
                        disableManagement(app);
                    } catch (Throwable t) {
                        Log.e(TAG, "exitnode: disableManagement crashed", t);
                    }
                }
            });
        } catch (Throwable t) {
            Log.e(TAG, "exitnode: disableManagementAsync не удалось поставить задачу", t);
        }
    }

    /**
     * Exit node реально применён: Tailscale активен и в его сети есть default route.
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
            // Показываем имя, для которого отправляли команду: текущее имя могло
            // быть изменено и ещё не применено.
            String appliedName = trimmed(prefs.getExitNodeAppliedName());
            sb.append(context.getString(R.string.exit_state_on,
                    appliedName.length() > 0 ? appliedName : prefs.getExitNodeName()));
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

        if (!exitActive && prefs.getExitConfirmAt() > 0L) {
            sb.append(" • ");
            sb.append(context.getString(R.string.exit_state_confirm));
        } else if (!exitActive && !tailscaleActive && prefs.getExitNodePendingOn()) {
            // Pending показываем только пока Tailscale не подключён: при активном
            // Tailscale ближайший tick либо применит команду, либо снимет pending.
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

        confirmIfDue(context, prefs);
        reconcile(context, prefs);
        consumePending(context, prefs);
        runWatchdog(context, prefs);
        runRetry(context, prefs);
    }

    /** Периодическая сверка желаемого и фактического состояния exit node. */
    private static void reconcile(Context context, Prefs prefs) {
        String name = trimmed(prefs.getExitNodeName());
        NetKind kind = NetworkUtils.currentKind(context);

        if (name.length() == 0) {
            // Имя очистили: нода, включённая приложением, больше не нужна.
            // Снимаем её по фактическому признаку, а не только по applied:
            // команда могла не подтвердиться, но успеть примениться.
            if ((prefs.isExitNodeApplied()
                    || prefs.getExitNodeAppliedName().length() > 0)
                    && isTailscaleActive(context)) {
                applyOff(context, prefs, "имя exit node очищено", false);
            } else {
                prefs.setExitNodeApplied(false);
                prefs.setExitNodeAppliedName("");
            }
            cancelPendingAndRetry(prefs);
            prefs.setExitConfirmAt(0L);
            return;
        }

        if (kind == NetKind.WIFI) {
            Logic.Trust trust = NetworkUtils.currentWifiAccess(context).trust;
            if (trust == Logic.Trust.TRUSTED) {
                handleTrustedWifi(context, prefs);
                return;
            }
            Log.d(TAG, "exitnode: Wi-Fi trust=" + trust + ", нужно включить exit node");
            ensureUntrustedWifi(context, prefs, name);
            return;
        }

        // Мобильная сеть, Ethernet и отсутствие сети: Tailscale с exit node —
        // не наш сценарий. Только сбрасываем отложенные состояния.
        cancelPendingAndRetry(prefs);
        prefs.setExitConfirmAt(0L);
        Log.d(TAG, "exitnode: сеть " + kind + ", правило exit node не действует");
    }

    /** Доверенная Wi-Fi: снять exit node, если его включало приложение. */
    private static void handleTrustedWifi(Context context, Prefs prefs) {
        // applied_name непусто — команда когда-то отправлялась, даже если
        // подтверждение не пришло: снятие в этом случае тоже нужно.
        boolean commandPending = prefs.isExitNodeApplied()
                || prefs.getExitConfirmAt() > 0L
                || prefs.getExitNodeAppliedName().length() > 0;
        cancelPendingAndRetry(prefs);
        prefs.setExitConfirmAt(0L);
        if (!commandPending) {
            return;
        }
        if (!isTailscaleActive(context)) {
            prefs.setExitNodeApplied(false);
            return;
        }
        applyOff(context, prefs, "доверенная Wi-Fi", false);
    }

    /** Незнакомая Wi-Fi: exit node должен быть включён. */
    private static void ensureUntrustedWifi(Context context, Prefs prefs, String name) {
        if (!isTailscaleActive(context)) {
            if (!prefs.getExitNodePendingOn() && !prefs.isExitRetryBlocked()) {
                applyOn(context, prefs, "правило Wi-Fi");
            }
            return;
        }
        // Имя поменяли, пока работала прежняя нода: команда с новым именем
        // заменит её. Проверить конкретную ноду нельзя — подтверждение идёт
        // по default route, поэтому просто отправляем команду заново.
        if (prefs.isExitNodeApplied() && !name.equals(prefs.getExitNodeAppliedName())) {
            applyOn(context, prefs, "имя изменилось");
            return;
        }
        if (isExitNodeActive(context)) {
            if (!prefs.isExitNodeApplied()) {
                if (prefs.getExitRetryAt() > 0L || prefs.isExitRetryBlocked()) {
                    // Идёт retry или автовосстановление остановлено: активный
                    // маршрут может быть и поздним включением, и несостоявшимся
                    // снятием. Решение — в runRetry либо за пользователем.
                    return;
                }
                // Маршрут появился позже подтверждения: команда всё-таки
                // применилась, берём exit node под watchdog.
                markApplied(context, prefs);
                return;
            }
            if (prefs.getExitNodePendingOn() || prefs.getExitRetryAt() > 0L
                    || prefs.isExitRetryBlocked()) {
                cancelPendingAndRetry(prefs);
            }
            return;
        }
        if (prefs.getExitConfirmAt() > 0L || prefs.getExitRetryAt() > 0L
                || prefs.isExitRetryBlocked()) {
            Log.d(TAG, "exitnode: " + Logic.maskHost(name) + " уже применяется или ждёт повтора");
            return;
        }
        applyOn(context, prefs, "правило Wi-Fi");
    }

    /**
     * Потребление exit_node_pending_on: Tailscale подключился — применяем команду.
     * Вызывается после reconcile, поэтому pending, сброшенный сменой правила,
     * сюда уже не попадает.
     */
    private static void consumePending(Context context, Prefs prefs) {
        if (!prefs.getExitNodePendingOn()) {
            return;
        }
        if (trimmed(prefs.getExitNodeName()).length() == 0) {
            prefs.setExitNodePendingOn(false);
            return;
        }
        if (!isTailscaleActive(context)) {
            return;
        }
        applyOn(context, prefs, "отложенное включение");
    }

    /** Watchdog: проверяет интернет через exit node и снимает его при отказе. */
    private static void runWatchdog(Context context, Prefs prefs) {
        if (!prefs.isExitWatchdogEnabled()) {
            return;
        }
        // Watchdog работает только для exit node, включённого приложением.
        if (!prefs.isExitNodeApplied()) {
            return;
        }
        if (prefs.getExitConfirmAt() > 0L || prefs.getExitRetryAt() > 0L) {
            return;
        }
        if (!isExitNodeActive(context)) {
            return;
        }
        if (NetworkUtils.currentKind(context) == NetKind.NONE) {
            return;
        }

        long nowRealtime = SystemClock.elapsedRealtime();
        boolean inGrace = nowRealtime - PROCESS_STARTED_AT < WATCHDOG_GRACE_MS
                || (lastOnCommandAt >= 0L
                && nowRealtime - lastOnCommandAt < WATCHDOG_GRACE_MS);
        if (inGrace) {
            return;
        }

        // В Doze и при выключенном экране пробы будят радио и дают ложные сбои:
        // пропускаем тик, не увеличивая счётчик неудач.
        if (!NetworkUtils.backgroundProbesAllowed(context)) {
            Log.i(TAG, "exitnode: watchdog пропущен — экран выключен или Doze");
            return;
        }
        if (!NetworkUtils.hasValidatedUnderlyingNetwork(context)) {
            Log.i(TAG, "exitnode: watchdog пропущен — сеть не validated");
            return;
        }

        if (NetworkUtils.probeInternet(context)) {
            if (prefs.getExitFailCount() != 0) {
                prefs.setExitFailCount(0);
                Log.i(TAG, "exitnode: интернет через exit node восстановился");
            }
            return;
        }

        int fails = prefs.getExitFailCount() + 1;
        prefs.setExitFailCount(fails);
        Log.i(TAG, "exitnode: watchdog, интернета нет (" + fails + "/"
                + WATCHDOG_FAILS_TO_TRIP + ")");
        if (fails < WATCHDOG_FAILS_TO_TRIP) {
            return;
        }

        applyOff(context, prefs, "нет интернета", true);
        prefs.setExitFailCount(0);
        // Первая попытка вернуть exit node — через 5 минут, дальше backoff.
        prefs.setExitRetryCount(1);
        prefs.setExitRetryAt(System.currentTimeMillis() + Logic.retryDelayMs(1));
        Log.i(TAG, "exitnode: exit node снят, повтор через "
                + (Logic.retryDelayMs(1) / 60000L) + " мин");
    }

    /** Автовосстановление exit node после снятия watchdog'ом, с backoff. */
    private static void runRetry(Context context, Prefs prefs) {
        long retryAt = prefs.getExitRetryAt();
        if (retryAt == 0L || System.currentTimeMillis() < retryAt) {
            return;
        }

        boolean exitActive = isExitNodeActive(context);
        if (exitActive) {
            if (!NetworkUtils.backgroundProbesAllowed(context)
                    || !NetworkUtils.hasValidatedUnderlyingNetwork(context)) {
                Log.i(TAG, "exitnode: retry отложен — экран выключен, Doze или сеть не validated");
                return;
            }
            if (NetworkUtils.probeInternet(context)) {
                if (!prefs.isExitNodeApplied()) {
                    // Позднее включение после сбоя подтверждения.
                    Log.i(TAG, "exitnode: exit node активен, подтверждаю после задержки");
                    markApplied(context, prefs);
                } else {
                    cancelPendingAndRetry(prefs);
                    Log.i(TAG, "exitnode: интернет есть, отменяю повтор");
                }
                return;
            }
            // Exit node всё ещё активен, но интернета через него нет: скорее
            // всего не применилось снятие. Повторяем снятие с backoff.
            Log.i(TAG, "exitnode: интернета всё ещё нет, повторяю снятие");
            int attempt = prefs.getExitRetryCount() + 1;
            applyOff(context, prefs, "нет интернета", true);
            scheduleRetryOrGiveUp(context, prefs, "Exit node: нет интернета", attempt);
            return;
        }

        if (!isUntrustedWifi(context)) {
            // Правило сменилось (доверенная Wi-Fi/мобильная): автовосстановление
            // больше не нужно.
            cancelPendingAndRetry(prefs);
            Log.i(TAG, "exitnode: retry отменён — exit node не нужен в текущей сети");
            return;
        }
        if (prefs.isExitRetryBlocked()) {
            return;
        }

        String host = trimmed(prefs.getExitNodeIp());
        if (host.length() == 0) {
            giveUp(context, prefs, "Exit node: не задан IP сервера —"
                    + " автовосстановление остановлено, проверьте вручную");
            return;
        }

        if (!NetworkUtils.backgroundProbesAllowed(context)) {
            Log.i(TAG, "exitnode: retry отложен — экран выключен или Doze");
            return;
        }
        if (!NetworkUtils.hasValidatedUnderlyingNetwork(context)) {
            Log.i(TAG, "exitnode: retry отложен — сеть не validated");
            return;
        }

        if (probeExitServer(context, prefs)) {
            cancelPendingAndRetry(prefs);
            applyOn(context, prefs, "восстановление после watchdog");
            return;
        }
        scheduleRetryOrGiveUp(context, prefs, "Exit node: сервер недоступен",
                prefs.getExitRetryCount() + 1);
    }

    /** Следующая попытка по backoff или остановка автовосстановления. */
    private static void scheduleRetryOrGiveUp(Context context, Prefs prefs, String reason,
                                              int attempt) {
        if (attempt >= MAX_RETRY_ATTEMPTS) {
            giveUp(context, prefs, "Exit node: не удалось восстановить exit node,"
                    + " проверьте вручную");
            return;
        }
        prefs.setExitRetryCount(attempt);
        long delay = Logic.retryDelayMs(attempt);
        prefs.setExitRetryAt(System.currentTimeMillis() + delay);
        String status = reason + ", повтор через " + (delay / 60000L) + " мин";
        Log.i(TAG, "exitnode: " + status);
        prefs.record(status, false, null);
        Notifications.updateMonitor(context);
    }

    /** Исчерпаны попытки: снять retry и показать ошибку с требованием ручной проверки. */
    private static void giveUp(Context context, Prefs prefs, String message) {
        prefs.setExitRetryAt(0L);
        prefs.setExitRetryCount(0);
        prefs.setExitRetryBlocked(true);
        Log.i(TAG, "exitnode: " + message);
        prefs.record(message, false, message);
        Notifications.updateMonitor(context);
        Notifications.showError(context, message, NetworkUtils.PKG_TAILSCALE);
    }

    /** Фиксирует подтверждённое включение exit node и снимает ошибки. */
    private static void markApplied(Context context, Prefs prefs) {
        // Имя могло измениться за 12 секунд подтверждения: сохраняем то, для
        // которого отправляли команду, иначе UI и снятие будут работать с
        // чужой нодой.
        String appliedName = trimmed(prefs.getExitNodeAppliedName());
        if (appliedName.length() == 0) {
            appliedName = trimmed(prefs.getExitNodeName());
            prefs.setExitNodeAppliedName(appliedName);
        }
        prefs.setExitNodeApplied(true);
        prefs.setExitNodePendingOn(false);
        prefs.setExitConfirmAt(0L);
        prefs.setExitRetryAt(0L);
        prefs.setExitRetryCount(0);
        prefs.setExitRetryBlocked(false);
        prefs.setExitFailCount(0);
        String status = "Exit node включён: " + appliedName;
        Log.d(TAG, "exitnode: подтверждён " + Logic.maskHost(appliedName));
        prefs.record(status, true, null);
        Notifications.clearError(context);
        Notifications.updateMonitor(context);
    }

    /** Подтверждение включения exit node по фактическому default route. */
    private static void confirmIfDue(Context context, Prefs prefs) {
        long confirmAt = prefs.getExitConfirmAt();
        if (confirmAt <= 0L || System.currentTimeMillis() < confirmAt) {
            return;
        }
        if (isExitNodeActive(context)) {
            prefs.setExitConfirmAt(0L);
            markApplied(context, prefs);
            return;
        }

        if (trimmed(prefs.getExitNodeName()).length() == 0) {
            // Имя убрали, пока ждали подтверждение: это не ошибка команды.
            // applied не сбрасываем — следующий reconcile снимет ноду по
            // пустому имени, даже если прежняя нода осталась активной.
            prefs.setExitConfirmAt(0L);
            return;
        }
        if (!isUntrustedWifi(context)) {
            // Сеть сменилась: правило больше не требует exit node. Флаг не
            // сбрасываем — reconcile снимет exit node на trusted Wi-Fi.
            Log.i(TAG, "exitnode: подтверждение отменено — сеть не требует exit node");
            return;
        }
        if (!isTailscaleActive(context)) {
            // Tailscale отключился за время ожидания: вернёмся в pending и
            // применим команду после его подключения, без ошибки.
            prefs.setExitConfirmAt(0L);
            prefs.setExitNodeApplied(false);
            prefs.setExitNodePendingOn(true);
            prefs.setExitRetryAt(0L);
            prefs.setExitRetryCount(0);
            Log.i(TAG, "exitnode: подтверждение отложено — Tailscale не активен");
            return;
        }

        prefs.setExitConfirmAt(0L);
        prefs.setExitNodeApplied(false);
        String name = trimmed(prefs.getExitNodeName());
        int attempt = prefs.getExitRetryCount() + 1;
        if (attempt >= MAX_RETRY_ATTEMPTS) {
            giveUp(context, prefs, "Exit node: не удалось восстановить exit node,"
                    + " проверьте вручную");
            return;
        }
        prefs.setExitRetryCount(attempt);
        long delay = Logic.retryDelayMs(attempt);
        prefs.setExitRetryAt(System.currentTimeMillis() + delay);
        String message = "Exit node " + name + ": команда не подтверждена."
                + " Проверьте имя ноды, повтор через " + (delay / 60000L) + " мин";
        Log.d(TAG, "exitnode: команда не подтверждена для " + Logic.maskHost(name)
                + ", повтор через " + (delay / 60000L) + " мин");
        prefs.record(message, false, message);
        Notifications.updateMonitor(context);
        Notifications.showError(context, message, NetworkUtils.PKG_TAILSCALE);
    }

    private static void applyOn(Context context, Prefs prefs, String reason) {
        String name = trimmed(prefs.getExitNodeName());
        if (name.length() == 0) {
            prefs.setExitNodePendingOn(false);
            prefs.setExitConfirmAt(0L);
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
            prefs.setExitConfirmAt(0L);
            prefs.setExitRetryAt(0L);
            prefs.setExitRetryCount(0);
            prefs.setExitRetryBlocked(false);
            String status = "Exit node " + name + ": ожидание подключения Tailscale";
            Log.d(TAG, "exitnode: ожидание Tailscale для " + Logic.maskHost(name)
                    + " (" + reason + ")");
            prefs.record(status, true, null);
            Notifications.updateMonitor(context);
            return;
        }

        if (!sendExitNode(context, name, prefs.isExitNodeAllowLan())) {
            prefs.setExitNodePendingOn(false);
            prefs.setExitNodeApplied(false);
            prefs.setExitNodeAppliedName("");
            prefs.setExitConfirmAt(0L);
            prefs.setExitRetryAt(0L);
            prefs.setExitRetryCount(0);
            // Получатель не объявлен: это не transient-сбой, повторять каждый
            // tick бессмысленно. Разблокирует ручная кнопка или смена сети.
            prefs.setExitRetryBlocked(true);
            String message = "Exit node " + name + ": Tailscale не принимает команду";
            Log.d(TAG, "exitnode: Tailscale не принял команду для "
                    + Logic.maskHost(name) + " (" + reason + ")");
            prefs.record(message, false, message);
            Notifications.updateMonitor(context);
            Notifications.showError(context, message, NetworkUtils.PKG_TAILSCALE);
            return;
        }

        long now = System.currentTimeMillis();
        lastOnCommandAt = SystemClock.elapsedRealtime();
        // Успех не фиксируем: команда лишь отправлена. applied появится только
        // после фактического default route в подтверждении. Имя запоминаем:
        // за время подтверждения его могут изменить.
        prefs.setExitNodeApplied(false);
        prefs.setExitNodeAppliedName(name);
        prefs.setExitNodePendingOn(false);
        prefs.setExitRetryAt(0L);
        prefs.setExitRetryCount(0);
        prefs.setExitRetryBlocked(false);
        prefs.setExitFailCount(0);
        prefs.setExitConfirmAt(now + CONFIRM_TIMEOUT_MS);
        String status = "Exit node " + name + ": команда отправлена, проверяю…";
        Log.d(TAG, "exitnode: команда отправлена для " + Logic.maskHost(name)
                + " (" + reason + ")");
        prefs.recordNeutral(status);
        Notifications.updateMonitor(context);
        scheduleConfirmation(context);
    }

    private static void applyOff(Context context, Prefs prefs, String reason, boolean error) {
        boolean tailscaleActive = isTailscaleActive(context);
        boolean delivered = true;
        if (tailscaleActive) {
            delivered = sendExitNode(context, "", prefs.isExitNodeAllowLan());
            if (!delivered) {
                Log.i(TAG, "exitnode: Tailscale не принял команду снятия");
            }
        } else {
            Log.i(TAG, "exitnode: Tailscale не подключён, broadcast снятия не нужен");
        }

        prefs.setExitNodeApplied(false);
        prefs.setExitNodeAppliedName("");
        prefs.setExitNodePendingOn(false);
        prefs.setExitRetryAt(0L);
        prefs.setExitRetryCount(0);
        prefs.setExitRetryBlocked(false);
        prefs.setExitConfirmAt(0L);
        prefs.setExitFailCount(0);

        String status = reason == null || reason.length() == 0
                ? "Exit node снят"
                : "Exit node снят: " + reason;
        if (!delivered) {
            status += " (команда не доставлена)";
        }
        boolean ok = delivered && !error;
        Log.i(TAG, "exitnode: " + status);
        prefs.record(status, ok, ok ? null : status);
        Notifications.updateMonitor(context);
        if (!ok) {
            Notifications.showError(context, status, NetworkUtils.PKG_TAILSCALE);
        }
    }

    /** Явное выключение управления: очистка флагов и снятие exit node. */
    private static void disableManagement(Context context) {
        Prefs prefs = new Prefs(context);
        boolean hadState = prefs.isExitNodeApplied()
                || prefs.getExitNodePendingOn()
                || prefs.getExitRetryAt() > 0L
                || prefs.getExitConfirmAt() > 0L
                || prefs.isExitRetryBlocked();
        boolean commandPending = prefs.isExitNodeApplied()
                || prefs.getExitConfirmAt() > 0L
                || prefs.getExitNodeAppliedName().length() > 0;

        prefs.setExitNodePendingOn(false);
        prefs.setExitRetryAt(0L);
        prefs.setExitRetryCount(0);
        prefs.setExitRetryBlocked(false);
        prefs.setExitFailCount(0);
        prefs.setExitConfirmAt(0L);

        if (commandPending && isTailscaleActive(context)) {
            if (!sendExitNode(context, "", prefs.isExitNodeAllowLan())) {
                Log.w(TAG, "exitnode: Tailscale не принял команду снятия при"
                        + " выключении управления");
            }
        }
        prefs.setExitNodeApplied(false);
        prefs.setExitNodeAppliedName("");

        if (hadState) {
            prefs.recordNeutral("Управление exit node выключено");
            Notifications.updateMonitor(context);
        }
        Log.i(TAG, "exitnode: управление выключено, exit node "
                + (commandPending ? "снят" : "не был включён приложением"));
    }

    /** Планирует пробуждение state machine через таймаут подтверждения. */
    private static void scheduleConfirmation(final Context context) {
        final Context app = context.getApplicationContext();
        try {
            SCHEDULER.schedule(new Runnable() {
                @Override
                public void run() {
                    try {
                        EXECUTOR.execute(new Runnable() {
                            @Override
                            public void run() {
                                try {
                                    // Полный tick, а не только confirmIfDue: если сеть
                                    // сменилась, reconcile сразу применит правило.
                                    tick(app);
                                } catch (Throwable t) {
                                    Log.e(TAG, "exitnode: confirm crashed", t);
                                }
                            }
                        });
                    } catch (Throwable t) {
                        Log.e(TAG, "exitnode: confirm не удалось поставить задачу", t);
                    }
                }
            }, CONFIRM_TIMEOUT_MS, TimeUnit.MILLISECONDS);
        } catch (Throwable t) {
            Log.e(TAG, "exitnode: scheduleConfirmation failed", t);
        }
    }

    /** TCP-проба сервера exit node: сначала через Tailscale, затем через активную сеть. */
    private static boolean probeExitServer(Context context, Prefs prefs) {
        String host = trimmed(prefs.getExitNodeIp());
        if (host.length() == 0) {
            Log.i(TAG, "exitnode: не задан IP сервера для проверки");
            return false;
        }

        Network vpn = isTailscaleActive(context)
                ? NetworkUtils.findVpnNetwork(context, VpnType.TAILSCALE)
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
                Log.d(TAG, "exitnode: проверяю " + Logic.maskHost(host) + ":" + port);
                if (NetworkUtils.probeTcp(network, host, port, PROBE_TIMEOUT_MS)) {
                    Log.d(TAG, "exitnode: сервер доступен " + Logic.maskHost(host)
                            + ":" + port);
                    return true;
                }
            }
        }
        Log.d(TAG, "exitnode: сервер " + Logic.maskHost(host) + " недоступен");
        return false;
    }

    /**
     * Отправляет broadcast Tailscale. Пустой список получателей — не повод
     * отказываться от команды: некоторые сборки Tailscale не объявляют
     * receiver через queryBroadcastReceivers, но принимают broadcast. Результат
     * подтверждается отдельной проверкой default route (12 с).
     */
    private static boolean sendExitNode(Context context, String exitNode, boolean allowLanAccess) {
        try {
            Intent intent = new Intent(ACTION_USE_EXIT_NODE);
            intent.setPackage(NetworkUtils.PKG_TAILSCALE);
            intent.putExtra(EXTRA_EXIT_NODE, exitNode == null ? "" : exitNode);
            intent.putExtra(EXTRA_ALLOW_LAN_ACCESS, allowLanAccess);
            List<ResolveInfo> receivers = context.getPackageManager()
                    .queryBroadcastReceivers(intent, 0);
            int receiverCount = receivers == null ? 0 : receivers.size();
            if (receiverCount == 0) {
                Log.w(TAG, "exitnode: Tailscale не объявляет получателя "
                        + ACTION_USE_EXIT_NODE + ", отправляю broadcast без подтверждения"
                        + " списка");
            }
            context.sendBroadcast(intent);
            Log.d(TAG, "exitnode: broadcast " + ACTION_USE_EXIT_NODE
                    + " exitNode=\"" + Logic.maskHost(exitNode) + "\""
                    + " allowLanAccess=" + allowLanAccess
                    + ", получателей: " + receiverCount);
            return true;
        } catch (SecurityException e) {
            Log.e(TAG, "exitnode: broadcast " + ACTION_USE_EXIT_NODE + " запрещён", e);
            return false;
        } catch (Throwable t) {
            Log.e(TAG, "exitnode: broadcast " + ACTION_USE_EXIT_NODE + " failed", t);
            return false;
        }
    }

    /** Сбрасывает отложенные состояния правила, не трогая applied. */
    private static void cancelPendingAndRetry(Prefs prefs) {
        prefs.setExitNodePendingOn(false);
        prefs.setExitRetryAt(0L);
        prefs.setExitRetryCount(0);
        prefs.setExitRetryBlocked(false);
        prefs.setExitFailCount(0);
    }

    private static boolean isUntrustedWifi(Context context) {
        if (NetworkUtils.currentKind(context) != NetKind.WIFI) {
            return false;
        }
        // Недоступный SSID/BSSID, UNBOUND и MISMATCH считаются незнакомой
        // сетью — как в VpnController.apply.
        return NetworkUtils.currentWifiAccess(context).trust != Logic.Trust.TRUSTED;
    }

    private static boolean isTailscaleActive(Context context) {
        VpnState vpn = NetworkUtils.detectVpn(context);
        return vpn.active && vpn.type == VpnType.TAILSCALE;
    }

    private static String trimmed(String value) {
        return value == null ? "" : value.trim();
    }
}
