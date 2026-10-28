package com.vpnswitch;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.graphics.drawable.Icon;
import android.os.Build;
import android.os.SystemClock;
import android.util.Log;

/** Каналы и уведомления приложения. */
public final class Notifications {

    private static final String TAG = "VpnSwitcher";

    public static final String CHANNEL_MONITOR = "vpn_monitor";
    public static final String CHANNEL_ALERTS = "vpn_alerts";
    /** Важные алерты: высокий приоритет, звук и вибрация по умолчанию. */
    public static final String CHANNEL_ALERTS_V2 = "vpn_alerts_v2";

    public static final int NOTIF_MONITOR = 1001;
    public static final int NOTIF_ERROR = 1002;

    /** Снимок для уведомления живёт не дольше 25 с: tick сервиса обновляет его. */
    private static final long MONITOR_CACHE_TTL_MS = 25000L;

    private static final int REQ_MONITOR_CONTENT = 100;
    private static final int REQ_MONITOR_APPLY = 200;
    private static final int REQ_ERROR_CONTENT = 300;
    private static final int REQ_ERROR_ACTION = 301;

    private static final int FLAGS = PendingIntent.FLAG_IMMUTABLE
            | PendingIntent.FLAG_UPDATE_CURRENT;

    private Notifications() {
    }

    public static void ensureChannels(Context context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            return;
        }
        try {
            NotificationManager nm =
                    (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm == null) {
                return;
            }

            NotificationChannel monitor = new NotificationChannel(
                    CHANNEL_MONITOR,
                    context.getString(R.string.channel_monitor_name),
                    NotificationManager.IMPORTANCE_LOW);
            monitor.setDescription(context.getString(R.string.channel_monitor_desc));
            monitor.setShowBadge(false);
            monitor.setSound(null, null);
            monitor.enableVibration(false);
            nm.createNotificationChannel(monitor);

            NotificationChannel alerts = new NotificationChannel(
                    CHANNEL_ALERTS,
                    context.getString(R.string.channel_alerts_name),
                    NotificationManager.IMPORTANCE_DEFAULT);
            alerts.setDescription(context.getString(R.string.channel_alerts_desc));
            alerts.setShowBadge(true);
            alerts.setSound(null, null);
            alerts.enableVibration(false);
            nm.createNotificationChannel(alerts);

            // Новый канал для важных ошибок: старый оставлен для совместимости,
            // но больше не используется.
            NotificationChannel alertsV2 = new NotificationChannel(
                    CHANNEL_ALERTS_V2,
                    context.getString(R.string.channel_alerts_v2_name),
                    NotificationManager.IMPORTANCE_HIGH);
            alertsV2.setDescription(context.getString(R.string.channel_alerts_desc));
            alertsV2.setShowBadge(true);
            alertsV2.enableVibration(true);
            nm.createNotificationChannel(alertsV2);
        } catch (Throwable t) {
            Log.e(TAG, "ensureChannels failed", t);
        }
    }

    /** Готовые строки уведомления; считаются не чаще, чем раз в 25 с. */
    private static final class MonitorSnapshot {
        final String currentLine;
        final String rule;
        final long at;

        MonitorSnapshot(String currentLine, String rule, long at) {
            this.currentLine = currentLine;
            this.rule = rule;
            this.at = at;
        }
    }

    private static volatile MonitorSnapshot cachedMonitorSnapshot;

    /**
     * Снимок состояния для мониторингового уведомления. При устаревании старше
     * 25 с пересчитывается один раз; частота обновления задаётся tick'ом сервиса.
     */
    private static MonitorSnapshot monitorSnapshot(Context context) {
        MonitorSnapshot cached = cachedMonitorSnapshot;
        long now = SystemClock.elapsedRealtime();
        if (cached != null && now - cached.at < MONITOR_CACHE_TTL_MS) {
            return cached;
        }
        return captureMonitorSnapshot(context);
    }

    /** Принудительно пересчитывает снимок: вызывается tick'ом MonitorService. */
    public static void refreshMonitorSnapshot(Context context) {
        cachedMonitorSnapshot = captureMonitorSnapshot(context);
    }

    private static MonitorSnapshot captureMonitorSnapshot(Context context) {
        long now = SystemClock.elapsedRealtime();
        NetKind kind = NetworkUtils.currentKind(context);
        VpnState vpn = NetworkUtils.detectVpn(context);
        NetworkUtils.WifiAccess access = kind == NetKind.WIFI
                ? NetworkUtils.currentWifiAccess(context, kind) : NetworkUtils.WifiAccess.none();
        MonitorSnapshot fresh = new MonitorSnapshot(
                StatusText.currentLine(kind, vpn),
                StatusText.desiredRule(context, kind, access),
                now);
        cachedMonitorSnapshot = fresh;
        return fresh;
    }

    /** Сбрасывает снимок уведомления: следующая сборка перечитает сеть. */
    public static void invalidateMonitorSnapshot() {
        cachedMonitorSnapshot = null;
    }

    /** Постоянное уведомление мониторинга с кнопкой «Применить». */
    public static Notification buildMonitor(Context context) {
        MonitorSnapshot snapshot = monitorSnapshot(context);
        Intent content = new Intent(context, MainActivity.class);
        content.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent contentPi =
                PendingIntent.getActivity(context, REQ_MONITOR_CONTENT, content, FLAGS);

        Intent apply = new Intent(context, MonitorService.class);
        apply.setAction(MonitorService.ACTION_APPLY);
        PendingIntent applyPi =
                PendingIntent.getForegroundService(context, REQ_MONITOR_APPLY, apply, FLAGS);

        Notification.Action applyAction = new Notification.Action.Builder(
                Icon.createWithResource(context, R.drawable.ic_notification),
                context.getString(R.string.action_apply),
                applyPi).build();

        return new Notification.Builder(context, CHANNEL_MONITOR)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(snapshot.currentLine)
                .setContentText(context.getString(R.string.notif_rule, snapshot.rule))
                .setContentIntent(contentPi)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setShowWhen(false)
                .setCategory(Notification.CATEGORY_SERVICE)
                .setColor(context.getColor(R.color.colorAccent))
                .addAction(applyAction)
                .build();
    }

    /** Обновляет мониторинговое уведомление, только пока сервис запущен. */
    public static void updateMonitor(Context context) {
        if (!MonitorService.isRunning()) {
            return;
        }
        try {
            NotificationManager nm =
                    (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null) {
                nm.notify(NOTIF_MONITOR, buildMonitor(context));
            }
        } catch (Throwable t) {
            Log.e(TAG, "updateMonitor failed", t);
        }
    }

    /**
     * Уведомление об ошибке с кнопкой открытия проблемного приложения.
     * targetPackage == null — открывается само приложение.
     */
    public static void showError(Context context, String text, String targetPackage) {
        try {
            Notifications.ensureChannels(context);
            if (!alertsEnabled(context)) {
                Log.w(TAG, "showError: уведомления выключены, сообщение не показано: " + text);
                return;
            }
            NotificationManager nm =
                    (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm == null) {
                return;
            }

            Intent open = openIntent(context, targetPackage);
            PendingIntent contentPi =
                    PendingIntent.getActivity(context, REQ_ERROR_CONTENT, open, FLAGS);
            PendingIntent actionPi =
                    PendingIntent.getActivity(context, REQ_ERROR_ACTION, open, FLAGS);

            String actionLabel;
            if (NetworkUtils.PKG_WDTT.equals(targetPackage)) {
                actionLabel = context.getString(R.string.action_open_wdtt);
            } else if (NetworkUtils.PKG_TAILSCALE.equals(targetPackage)) {
                actionLabel = context.getString(R.string.action_open_tailscale);
            } else {
                actionLabel = context.getString(R.string.action_open_app);
            }

            Notification.Action action = new Notification.Action.Builder(
                    Icon.createWithResource(context, R.drawable.ic_notification),
                    actionLabel,
                    actionPi).build();

            Notification notification = new Notification.Builder(context, CHANNEL_ALERTS_V2)
                    .setSmallIcon(R.drawable.ic_notification)
                    .setContentTitle(context.getString(R.string.notif_error_title))
                    .setContentText(text)
                    .setStyle(new Notification.BigTextStyle().bigText(text))
                    .setContentIntent(contentPi)
                    .setAutoCancel(true)
                    .setColor(context.getColor(R.color.colorRed))
                    .addAction(action)
                    .build();

            nm.notify(NOTIF_ERROR, notification);
        } catch (Throwable t) {
            Log.e(TAG, "showError failed", t);
        }
    }

    /** Убирает уведомление об ошибке после успешного восстановления VPN. */
    public static void clearError(Context context) {
        try {
            NotificationManager nm =
                    (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null) {
                nm.cancel(NOTIF_ERROR);
            }
        } catch (Throwable t) {
            Log.e(TAG, "clearError failed", t);
        }
    }

    /**
     * true, если уведомления приложения не заблокированы пользователем, выдано
     * POST_NOTIFICATIONS и канал важных алертов не отключён вручную.
     */
    public static boolean alertsEnabled(Context context) {
        if (context == null) {
            return false;
        }
        try {
            NotificationManager nm =
                    (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm == null || !nm.areNotificationsEnabled()) {
                return false;
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                NotificationChannel channel = nm.getNotificationChannel(CHANNEL_ALERTS_V2);
                // Канал ещё не создан (первый запуск) — считаем включённым:
                // ensureChannels создаст его с важностью по умолчанию.
                return channel == null
                        || channel.getImportance() != NotificationManager.IMPORTANCE_NONE;
            }
            return true;
        } catch (Throwable t) {
            Log.d(TAG, "alertsEnabled: " + t);
            return true;
        }
    }

    private static Intent openIntent(Context context, String targetPackage) {
        if (targetPackage != null) {
            try {
                Intent launch = context.getPackageManager()
                        .getLaunchIntentForPackage(targetPackage);
                if (launch != null) {
                    launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                            | Intent.FLAG_ACTIVITY_CLEAR_TOP);
                    return launch;
                }
            } catch (Throwable t) {
                Log.d(TAG, "openIntent: нет launch intent для " + targetPackage, t);
            }
        }
        Intent main = new Intent(context, MainActivity.class);
        main.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        return main;
    }
}
