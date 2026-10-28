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
        return currentLine(kind, vpn);
    }

    /** Та же строка по уже посчитанным kind и состоянию VPN. */
    public static String currentLine(NetKind kind, VpnState vpn) {
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
        NetworkUtils.WifiAccess access = kind == NetKind.WIFI
                ? NetworkUtils.currentWifiAccess(context, kind) : NetworkUtils.WifiAccess.none();
        return desiredRule(context, kind, access);
    }

    /** Текущее правило по готовому снимку сети и Wi-Fi. */
    public static String desiredRule(Context context, NetKind kind,
                                     NetworkUtils.WifiAccess access) {
        if (kind == NetKind.WIFI) {
            boolean trusted = access != null && access.trust == Logic.Trust.TRUSTED;
            return context.getString(trusted
                    ? R.string.rule_wifi_trusted
                    : R.string.rule_wifi_untrusted);
        }
        if (kind == NetKind.MOBILE) {
            return context.getString(R.string.rule_mobile_wdtt);
        }
        return context.getString(R.string.rule_wait_network);
    }

    /**
     * Строка состояния exit node по готовому снимку. Зеркалит
     * {@link ExitNodeController#stateLine(Context)}, но не пересканирует сети:
     * tsInstalled/tailscaleActive/exitActive уже посчитаны вызывающим.
     */
    public static String exitStateLine(Context context, Prefs prefs, boolean tsInstalled,
                                       boolean tailscaleActive, boolean exitActive) {
        if (!prefs.isExitManageEnabled()) {
            return context.getString(R.string.exit_state_disabled);
        }
        if (!tsInstalled) {
            return context.getString(R.string.exit_state_no_tailscale);
        }

        StringBuilder sb = new StringBuilder();
        if (exitActive) {
            // Имя, для которого отправляли команду: текущее могло быть изменено
            // и ещё не применено.
            String appliedName = prefs.getExitNodeAppliedName();
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
            sb.append(" • ");
            sb.append(context.getString(R.string.exit_state_pending));
        } else if (!exitActive && tailscaleActive && prefs.isExitNodeApplied()) {
            sb.append(" • ");
            sb.append(context.getString(R.string.exit_state_unconfirmed));
        }
        return sb.toString();
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
