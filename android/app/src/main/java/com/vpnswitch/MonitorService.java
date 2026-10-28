package com.vpnswitch;

import android.app.Notification;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.ConnectivityManager;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;

/** Foreground-сервис: следит за сетью и обновляет уведомление. */
public class MonitorService extends Service {

    private static final String TAG = "VpnSwitcher";

    public static final String ACTION_APPLY = "com.vpnswitch.action.APPLY";
    public static final String ACTION_STOP = "com.vpnswitch.action.STOP";

    private static final long NETWORK_DEBOUNCE_MS = 3000L;
    private static final long START_DEBOUNCE_MS = 1200L;
    private static final long TICK_MS = 30000L;

    private static volatile boolean running = false;

    public static boolean isRunning() {
        return running;
    }

    public static void refreshNotification(Context context) {
        if (running) {
            Notifications.updateMonitor(context);
        }
    }

    private final Handler handler = new Handler(Looper.getMainLooper());

    private ConnectivityManager connectivityManager;
    private ConnectivityManager.NetworkCallback networkCallback;
    private ConnectivityManager.NetworkCallback wifiNetworkCallback;
    private Prefs prefs;
    private boolean applyPending = false;
    private boolean pendingManual = false;

    private final Runnable applyRunnable = new Runnable() {
        @Override
        public void run() {
            applyPending = false;
            boolean manual = pendingManual;
            pendingManual = false;

            if (!manual && !new Prefs(MonitorService.this).isAutoEnabled()) {
                Log.i(TAG, "MonitorService: автопереключение выключено, apply не нужен");
                return;
            }
            if (VpnController.isApplying()) {
                Log.i(TAG, "MonitorService: контроллер занят, повтор через 3 с");
                scheduleApply(NETWORK_DEBOUNCE_MS, manual);
                return;
            }
            VpnController.applyAsync(MonitorService.this,
                    manual ? "notification" : "network", manual);
        }
    };

    private final Runnable tickRunnable = new Runnable() {
        @Override
        public void run() {
            if (!running) {
                return;
            }
            ExitNodeController.tickAsync(MonitorService.this);
            reconcilePeriodic();
            Notifications.refreshMonitorSnapshot(MonitorService.this);
            Notifications.updateMonitor(MonitorService.this);
            handler.postDelayed(this, TICK_MS);
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        Notifications.ensureChannels(this);
        prefs = new Prefs(this);
        if (!enabledByPrefs()) {
            // Ни автопереключение, ни управление exit node не включены: сервис
            // не должен висеть в foreground и крутить tick.
            Log.i(TAG, "MonitorService: auto и exit node выключены, сервис не нужен");
            running = false;
            stopSelf();
            return;
        }
        // Стартовую сеть только запоминаем: переходом считается лишь последующая
        // смена сети при работающем сервисе.
        prefs.setLastNetKind(NetworkUtils.currentKind(this));
        if (!startForegroundNow()) {
            reportForegroundFailure();
            return;
        }
        running = true;
        handler.postDelayed(tickRunnable, TICK_MS);
        registerNetworkCallback();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        prefs = prefs != null ? prefs : new Prefs(this);
        if (!enabledByPrefs()) {
            Log.i(TAG, "MonitorService: auto и exit node выключены, STOP");
            stopSelf();
            return START_NOT_STICKY;
        }
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            Log.i(TAG, "MonitorService: STOP");
            stopSelf();
            return START_NOT_STICKY;
        }

        // Сначала FGS: если он не поднялся, сервис будет остановлен, и тик не
        // должен успеть применить или снять exit node без присмотра.
        if (!startForegroundNow()) {
            reportForegroundFailure();
            return START_NOT_STICKY;
        }
        running = true;
        ExitNodeController.tickAsync(this);

        boolean manual = intent != null && ACTION_APPLY.equals(intent.getAction());
        scheduleApply(START_DEBOUNCE_MS, manual);
        return START_STICKY;
    }

    private boolean enabledByPrefs() {
        if (prefs == null) {
            prefs = new Prefs(this);
        }
        return prefs.isAutoEnabled() || prefs.isExitManageEnabled();
    }

    @Override
    public void onDestroy() {
        running = false;
        handler.removeCallbacksAndMessages(null);
        if (connectivityManager != null && networkCallback != null) {
            try {
                connectivityManager.unregisterNetworkCallback(networkCallback);
            } catch (Throwable t) {
                Log.d(TAG, "MonitorService: unregisterNetworkCallback: " + t);
            }
        }
        if (connectivityManager != null && wifiNetworkCallback != null) {
            try {
                connectivityManager.unregisterNetworkCallback(wifiNetworkCallback);
            } catch (Throwable t) {
                Log.d(TAG, "MonitorService: unregister wifi callback: " + t);
            }
        }
        networkCallback = null;
        wifiNetworkCallback = null;
        connectivityManager = null;
        // Если сервис останавливают при обоих выключенных режимах, exit node,
        // включённый приложением, нужно снять: иначе он останется без присмотра.
        if (prefs != null && !prefs.isAutoEnabled() && !prefs.isExitManageEnabled()) {
            ExitNodeController.disableManagementAsync(this);
        }
        try {
            stopForeground(STOP_FOREGROUND_REMOVE);
        } catch (Throwable t) {
            Log.d(TAG, "MonitorService: stopForeground: " + t);
        }
        Log.i(TAG, "MonitorService: destroyed");
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    /**
     * Запуск foreground-режима с явным типом specialUse на Android 14+.
     * false — FGS не поднялся: сервис нельзя оставлять running, иначе система
     * убьёт его через несколько секунд, а UI будет показывать «активен».
     */
    private boolean startForegroundNow() {
        try {
            Notification n = Notifications.buildMonitor(this);
            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(Notifications.NOTIF_MONITOR, n,
                        ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
            } else {
                startForeground(Notifications.NOTIF_MONITOR, n);
            }
            return true;
        } catch (Throwable t) {
            Log.e(TAG, "MonitorService: startForeground failed", t);
            return false;
        }
    }

    /** FGS не запустился: снять running, записать ошибку и остановить сервис. */
    private void reportForegroundFailure() {
        running = false;
        String message = getString(R.string.error_foreground_failed);
        if (prefs == null) {
            prefs = new Prefs(this);
        }
        prefs.record(message, false, message);
        Log.e(TAG, "MonitorService: foreground не запущен, сервис остановлен");
        Notifications.showError(this, message, null);
        stopSelf();
    }

    private void registerNetworkCallback() {
        try {
            connectivityManager =
                    (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
            if (connectivityManager == null) {
                return;
            }
            networkCallback = new ConnectivityManager.NetworkCallback() {
                @Override
                public void onAvailable(Network network) {
                    Log.d(TAG, "network onAvailable: " + network);
                    handleNetworkEvent("onAvailable");
                }

                @Override
                public void onLost(Network network) {
                    Log.d(TAG, "network onLost: " + network);
                    handleNetworkEvent("onLost");
                }

                @Override
                public void onCapabilitiesChanged(Network network,
                                                  NetworkCapabilities capabilities) {
                    handleNetworkEvent("onCapabilitiesChanged");
                }

                @Override
                public void onLinkPropertiesChanged(Network network,
                                                    LinkProperties linkProperties) {
                    handleNetworkEvent("onLinkPropertiesChanged");
                }
            };
            connectivityManager.registerDefaultNetworkCallback(networkCallback, handler);
            Log.i(TAG, "MonitorService: network callback зарегистрирован");
        } catch (Throwable t) {
            Log.e(TAG, "MonitorService: registerNetworkCallback failed", t);
        }

        // Отдельный callback на Wi-Fi: default-сеть может остаться мобильной,
        // и тогда подключение Wi-Fi не вызовет default callback. Без него
        // currentKind уже WIFI, но сервис об этом не узнает.
        try {
            if (connectivityManager == null) {
                return;
            }
            wifiNetworkCallback = new ConnectivityManager.NetworkCallback() {
                @Override
                public void onAvailable(Network network) {
                    Log.d(TAG, "wifi onAvailable: " + network);
                    handleNetworkEvent("wifi:onAvailable");
                }

                @Override
                public void onLost(Network network) {
                    Log.d(TAG, "wifi onLost: " + network);
                    handleNetworkEvent("wifi:onLost");
                }

                @Override
                public void onCapabilitiesChanged(Network network,
                                                  NetworkCapabilities capabilities) {
                    handleNetworkEvent("wifi:onCapabilitiesChanged");
                }

                @Override
                public void onLinkPropertiesChanged(Network network,
                                                    LinkProperties linkProperties) {
                    handleNetworkEvent("wifi:onLinkPropertiesChanged");
                }
            };
            NetworkRequest wifiRequest = new NetworkRequest.Builder()
                    .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                    .build();
            connectivityManager.registerNetworkCallback(wifiRequest, wifiNetworkCallback, handler);
            Log.i(TAG, "MonitorService: wifi network callback зарегистрирован");
        } catch (Throwable t) {
            Log.e(TAG, "MonitorService: register wifi callback failed", t);
        }
    }

    /**
     * Общая обработка сетевых событий. При уходе с Wi-Fi Tailscale отключается сразу,
     * не дожидаясь debounce, чтобы он не остался включённым в мобильной сети.
     *
     * Если в момент ухода WDTT Plus был активен на доверенной сети, он, вероятно,
     * ушёл в свою паузу — запоминаем это, чтобы не отправлять toggle вслепую.
     * Точная семантика паузы WDTT требует проверки на устройстве.
     */
    private void handleNetworkEvent(String source) {
        if (prefs == null) {
            prefs = new Prefs(this);
        }
        Notifications.invalidateMonitorSnapshot();
        NetKind kind = NetworkUtils.currentKind(this);
        NetKind previous = prefs.getLastNetKind();
        if (previous == NetKind.WIFI && kind != NetKind.WIFI) {
            handleLeftWifi(source);
        }
        prefs.setLastNetKind(kind);
        if (prefs.isExitManageEnabled()) {
            // Правило exit node не зависит от auto: сверяем сеть всегда.
            ExitNodeController.onNetworkChangedAsync(this);
        }
        scheduleApply(NETWORK_DEBOUNCE_MS, false);
    }

    /**
     * Побочные эффекты ухода с Wi-Fi. Общий путь для callback'а и периодической
     * сверки: если callback потерян, сверка обязана сделать то же самое.
     */
    private void handleLeftWifi(String source) {
        prefs.setLeftWifiAt(System.currentTimeMillis());
        VpnState vpn = NetworkUtils.detectVpn(this);
        boolean stillActive = vpn.active && vpn.type == VpnType.WDTT;
        if (prefs.isWdttTrusted()
                && (prefs.isWdttActiveOnWifi() || stillActive)) {
            // WDTT был активен на Wi-Fi и мог уйти в авто-паузу — не шлём toggle
            // вслепую, даже если пауза наступила до/после этого события.
            prefs.setWdttPausedOnWifi(true);
            Log.i(TAG, "MonitorService: WDTT Plus мог уйти в паузу после Wi-Fi");
        }
        prefs.setWdttActiveOnWifi(false);
        VpnController.disconnectTailscale(this);
        Log.i(TAG, "MonitorService: уход с Wi-Fi (" + source
                + "), запрошено досрочное отключение Tailscale");
    }

    /**
     * Периодическая сверка вместо доверия только callback'ам: сравнивает
     * сохранённый lastNetKind и фактическое правило с реальным состоянием.
     * Применение идёт через тот же applyRunnable/debounce, второго пути нет.
     */
    private void reconcilePeriodic() {
        if (prefs == null) {
            prefs = new Prefs(this);
        }
        NetKind kind = NetworkUtils.currentKind(this);
        NetKind previous = prefs.getLastNetKind();
        boolean changed = kind != previous;
        if (changed) {
            Log.i(TAG, "MonitorService: сеть изменилась без callback: "
                    + previous + " -> " + kind);
            if (previous == NetKind.WIFI && kind != NetKind.WIFI) {
                // Callback потерян: повторяем те же побочные эффекты ухода.
                handleLeftWifi("reconcile");
            }
            prefs.setLastNetKind(kind);
            if (prefs.isExitManageEnabled()) {
                ExitNodeController.onNetworkChangedAsync(this);
            }
        }
        if (!prefs.isAutoEnabled()) {
            return;
        }
        if (kind != NetKind.WIFI && kind != NetKind.MOBILE) {
            // Ethernet/нет сети: применять нечего, ждём следующего события.
            return;
        }

        NetworkUtils.WifiAccess access = kind == NetKind.WIFI
                ? NetworkUtils.currentWifiAccess(this, kind) : NetworkUtils.WifiAccess.none();
        boolean trusted = access.trust == Logic.Trust.TRUSTED;
        VpnState vpn = NetworkUtils.detectVpn(this);
        if (kind == NetKind.WIFI) {
            if (!prefs.isWdttTrusted()) {
                prefs.setWdttActiveOnWifi(false);
            } else if (vpn.type == VpnType.WDTT) {
                // Флаг «липкий»: после авто-паузы WDTT на Wi-Fi он должен
                // сохраниться до ухода, иначе пауза снова потеряется.
                prefs.setWdttActiveOnWifi(true);
            }
        } else {
            // Вне Wi-Fi флаг не нужен: он относится только к уходу с Wi-Fi.
            prefs.setWdttActiveOnWifi(false);
        }
        boolean satisfied = Logic.ruleSatisfied(kind, vpn.active, vpn.type, trusted,
                false, false);
        if (changed || !satisfied) {
            if (!changed && lastActionFailed()) {
                // Постоянная ошибка (например, VPN не установлен) не должна
                // повторяться каждые 30 секунд: пользователь уже увидел алерт.
                Log.i(TAG, "MonitorService: правило не выполнено, прошлая ошибка — ждём");
                return;
            }
            Log.i(TAG, "MonitorService: правило не выполнено (kind=" + kind
                    + "), планирую apply");
            scheduleApply(NETWORK_DEBOUNCE_MS, false);
        }
    }

    /** Последняя записанная операция завершилась ошибкой. */
    private boolean lastActionFailed() {
        return prefs.getLastTime() > 0L && !prefs.isLastOk();
    }

    /** Пока задача ожидает выполнения, повторные события не сбрасывают таймер. */
    private void scheduleApply(long delayMs, boolean manual) {
        if (applyPending) {
            if (manual) {
                pendingManual = true;
            }
            Log.d(TAG, "MonitorService: apply уже запланирован");
            return;
        }
        applyPending = true;
        pendingManual = manual;
        handler.postDelayed(applyRunnable, delayMs);
        Log.d(TAG, "MonitorService: apply запланирован через " + delayMs + " мс, manual="
                + manual);
    }
}
