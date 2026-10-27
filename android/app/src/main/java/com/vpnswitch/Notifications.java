package com.vpnswitch;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.graphics.drawable.Icon;
import android.os.Build;
import android.util.Log;

/** Каналы и уведомления приложения. */
public final class Notifications {

    private static final String TAG = "VpnSwitcher";

    public static final String CHANNEL_MONITOR = "vpn_monitor";
    public static final String CHANNEL_ALERTS = "vpn_alerts";

    public static final int NOTIF_MONITOR = 1001;
    public static final int NOTIF_ERROR = 1002;

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
        } catch (Throwable t) {
            Log.e(TAG, "ensureChannels failed", t);
        }
    }

    /** Постоянное уведомление мониторинга с кнопкой «Применить». */
    public static Notification buildMonitor(Context context) {
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
                .setContentTitle(StatusText.currentLine(context))
                .setContentText(context.getString(R.string.notif_rule,
                        StatusText.desiredRule(context)))
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

            Notification notification = new Notification.Builder(context, CHANNEL_ALERTS)
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
