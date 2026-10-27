package com.vpnswitch;

/** Снимок состояния VPN: активен ли, чей это VPN и пакет-владелец, если известен. */
public final class VpnState {

    public final boolean active;
    public final VpnType type;
    public final String ownerPackage;

    public VpnState(boolean active, VpnType type, String ownerPackage) {
        this.active = active;
        this.type = type == null ? VpnType.NONE : type;
        this.ownerPackage = ownerPackage;
    }

    public static VpnState none() {
        return new VpnState(false, VpnType.NONE, null);
    }
}
