package com.vpnswitch;

import android.content.Context;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/** Формирование текстов состояния для UI и уведомления. */
public final class StatusText {

    private StatusText() {
    }

    /** Например: «Wi-Fi • Tailscale подключён». */
    public static String currentLine(Context context) {
        NetKind kind = NetworkUtils.currentKind(context);
        VpnState vpn = NetworkUtils.detectVpn(context);
        String network = NetworkUtils.kindLabel(kind);
        if (vpn.active && (vpn.type == VpnType.TAILSCALE || vpn.type == VpnType.WDTT)) {
            return network + " • " + NetworkUtils.vpnLabel(vpn) + " подключён";
        }
        if (vpn.active) {
            return network + " • " + NetworkUtils.vpnLabel(vpn) + " активен";
        }
        return network + " • " + NetworkUtils.vpnLabel(vpn);
    }

    /** Текущее правило по типу сети. */
    public static String desiredRule(Context context) {
        NetKind kind = NetworkUtils.currentKind(context);
        if (kind == NetKind.WIFI) {
            return context.getString(NetworkUtils.isTrustedWifi(context)
                    ? R.string.rule_wifi_trusted
                    : R.string.rule_wifi_untrusted);
        }
        if (kind == NetKind.MOBILE) {
            return context.getString(R.string.rule_mobile_wdtt);
        }
        return context.getString(R.string.rule_wait_network);
    }

    /** Последнее действие и время или «—». */
    public static String lastLine(Context context) {
        Prefs prefs = new Prefs(context);
        String status = prefs.getLastStatus();
        if (status == null || status.length() == 0) {
            return "—";
        }
        long time = prefs.getLastTime();
        if (time <= 0L) {
            return status;
        }
        String formatted;
        try {
            formatted = new SimpleDateFormat("HH:mm:ss", Locale.getDefault())
                    .format(new Date(time));
        } catch (Throwable t) {
            formatted = "";
        }
        if (formatted.length() == 0) {
            return status;
        }
        return status + " • " + formatted;
    }
}
