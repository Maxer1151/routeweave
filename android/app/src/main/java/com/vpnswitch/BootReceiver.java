package com.vpnswitch;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.util.Log;

/** Автозапуск мониторинга после загрузки и обновления приложения. */
public class BootReceiver extends BroadcastReceiver {

    private static final String TAG = "VpnSwitcher";

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent == null ? null : intent.getAction();
        Log.i(TAG, "BootReceiver: " + action);

        if (!Intent.ACTION_BOOT_COMPLETED.equals(action)
                && !Intent.ACTION_MY_PACKAGE_REPLACED.equals(action)) {
            return;
        }

        Prefs prefs = new Prefs(context);
        if (!prefs.isAutoEnabled() && !prefs.isExitManageEnabled()) {
            Log.i(TAG, "BootReceiver: автопереключение и exit node выключены");
            return;
        }

        try {
            Intent service = new Intent(context, MonitorService.class);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(service);
            } else {
                context.startService(service);
            }
            Log.i(TAG, "BootReceiver: MonitorService запущен");
        } catch (Throwable t) {
            // На Android 12+ фоновый запуск FGS может быть запрещён.
            Log.e(TAG, "BootReceiver: не удалось запустить MonitorService", t);
        }
    }
}
