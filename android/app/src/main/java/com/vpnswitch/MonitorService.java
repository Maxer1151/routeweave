package com.vpnswitch;

import android.app.Notification;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
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
            Notifications.updateMonitor(MonitorService.this);
            handler.postDelayed(this, TICK_MS);
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        Notifications.ensureChannels(this);
        prefs = new Prefs(this);
        // Стартовую сеть только запоминаем: переходом считается лишь последующая
        // смена сети при работающем сервисе.
        prefs.setLastNetKind(NetworkUtils.currentKind(this));
        startForegroundNow();
        running = true;
        handler.postDelayed(tickRunnable, TICK_MS);
        registerNetworkCallback();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        ExitNodeController.tickAsync(this);
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            Log.i(TAG, "MonitorService: STOP");
            stopSelf();
            return START_NOT_STICKY;
        }

        startForegroundNow();
        running = true;

        boolean manual = intent != null && ACTION_APPLY.equals(intent.getAction());
        scheduleApply(START_DEBOUNCE_MS, manual);
        return START_STICKY;
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
        networkCallback = null;
        connectivityManager = null;
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

    /** Запуск foreground-режима с явным типом specialUse на Android 14+. */
    private void startForegroundNow() {
        try {
            Notification n = Notifications.buildMonitor(this);
            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(Notifications.NOTIF_MONITOR, n,
                        ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
            } else {
                startForeground(Notifications.NOTIF_MONITOR, n);
            }
        } catch (Throwable t) {
            Log.e(TAG, "MonitorService: startForeground failed", t);
        }
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
            };
            connectivityManager.registerDefaultNetworkCallback(networkCallback, handler);
            Log.i(TAG, "MonitorService: network callback зарегистрирован");
        } catch (Throwable t) {
            Log.e(TAG, "MonitorService: registerNetworkCallback failed", t);
        }
    }

    /**
     * Общая обработка сетевых событий. При уходе с Wi-Fi Tailscale отключается сразу,
     * не дожидаясь debounce, чтобы он не остался включённым в мобильной сети.
     */
    private void handleNetworkEvent(String source) {
        if (prefs == null) {
            prefs = new Prefs(this);
        }
        NetKind kind = NetworkUtils.currentKind(this);
        NetKind previous = prefs.getLastNetKind();
        if (previous == NetKind.WIFI && kind != NetKind.WIFI) {
            prefs.setLeftWifiAt(System.currentTimeMillis());
            VpnController.disconnectTailscale(this);
            Log.i(TAG, "MonitorService: уход с Wi-Fi (" + source
                    + "), Tailscale отключён досрочно");
        }
        prefs.setLastNetKind(kind);
        scheduleApply(NETWORK_DEBOUNCE_MS, false);
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
