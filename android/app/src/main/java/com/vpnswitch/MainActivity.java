package com.vpnswitch;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.util.Log;
import android.view.View;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.CompoundButton;
import android.widget.EditText;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/** Единственный экран приложения. */
public class MainActivity extends Activity {

    private static final String TAG = "VpnSwitcher";
    private static final int REQ_POST_NOTIFICATIONS = 41;
    private static final int REQ_SSID_PERMISSION = 42;
    private static final int REQ_SSID_BACKGROUND_LOCATION = 43;
    private static final long UI_REFRESH_MS = 1500L;
    private static final String STATE_SSID_PERM_REQUESTED = "ssid_perm_requested";

    private final Handler handler = new Handler(Looper.getMainLooper());

    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            refreshUi();
            handler.postDelayed(this, UI_REFRESH_MS);
        }
    };

    /** Отложенное применение правила после изменения списка доверенных сетей. */
    private final Runnable applyTrustedSsids = new Runnable() {
        @Override
        public void run() {
            VpnController.applyAsync(MainActivity.this, "trusted_ssids", true);
        }
    };

    private Prefs prefs;

    private View viewStatusDot;
    private TextView tvStatusBadge;
    private TextView tvHeroNetwork;
    private TextView tvHeroVpn;
    private TextView tvHeroRule;
    private TextView tvHeroLast;
    private Button btnApply;

    private Switch swAuto;
    private TextView tvMonitorState;
    private CheckBox cbTrusted;

    private TextView tvCurrentSsid;
    private TextView tvSsidStatus;
    private EditText etTrustedSsids;
    private TextView tvSsidCount;
    private Button btnAddCurrent;
    private Button btnSaveSsids;
    private Button btnSsidPermission;

    private Button btnTsOn;
    private Button btnTsOff;
    private Button btnWdttOn;
    private Button btnWdttOff;
    private Button btnOpenTs;
    private Button btnOpenWdtt;

    private TextView tvDiagTailscale;
    private TextView tvDiagWdtt;
    private TextView tvDiagNotif;
    private TextView tvDiagBattery;
    private TextView tvDiagSsid;
    private Button btnBattery;

    private Switch swExitManage;
    private EditText etExitName;
    private EditText etExitIp;
    private CheckBox cbExitAllowLan;
    private CheckBox cbExitWatchdog;
    private Button btnExitOff;
    private Button btnExitOn;
    private TextView tvExitState;

    /** Запрашивали ли доступ к SSID в текущем экземпляре экрана. */
    private boolean ssidPermissionRequested;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        prefs = new Prefs(this);
        Notifications.ensureChannels(this);

        viewStatusDot = findViewById(R.id.view_status_dot);
        tvStatusBadge = findViewById(R.id.tv_status_badge);
        tvHeroNetwork = findViewById(R.id.tv_hero_network);
        tvHeroVpn = findViewById(R.id.tv_hero_vpn);
        tvHeroRule = findViewById(R.id.tv_hero_rule);
        tvHeroLast = findViewById(R.id.tv_hero_last);
        btnApply = findViewById(R.id.btn_apply);

        swAuto = findViewById(R.id.sw_auto);
        tvMonitorState = findViewById(R.id.tv_monitor_state);
        cbTrusted = findViewById(R.id.cb_trusted);

        tvCurrentSsid = findViewById(R.id.tv_current_ssid);
        tvSsidStatus = findViewById(R.id.tv_ssid_status);
        etTrustedSsids = findViewById(R.id.et_trusted_ssids);
        tvSsidCount = findViewById(R.id.tv_ssid_count);
        btnAddCurrent = findViewById(R.id.btn_add_current);
        btnSaveSsids = findViewById(R.id.btn_save_ssids);
        btnSsidPermission = findViewById(R.id.btn_ssid_permission);

        btnTsOn = findViewById(R.id.btn_ts_on);
        btnTsOff = findViewById(R.id.btn_ts_off);
        btnWdttOn = findViewById(R.id.btn_wdtt_on);
        btnWdttOff = findViewById(R.id.btn_wdtt_off);
        btnOpenTs = findViewById(R.id.btn_open_ts);
        btnOpenWdtt = findViewById(R.id.btn_open_wdtt);

        tvDiagTailscale = findViewById(R.id.tv_diag_tailscale);
        tvDiagWdtt = findViewById(R.id.tv_diag_wdtt);
        tvDiagNotif = findViewById(R.id.tv_diag_notif);
        tvDiagBattery = findViewById(R.id.tv_diag_battery);
        tvDiagSsid = findViewById(R.id.tv_diag_ssid);
        btnBattery = findViewById(R.id.btn_battery);

        swExitManage = findViewById(R.id.sw_exit_manage);
        etExitName = findViewById(R.id.et_exit_name);
        etExitIp = findViewById(R.id.et_exit_ip);
        cbExitAllowLan = findViewById(R.id.cb_exit_allow_lan);
        cbExitWatchdog = findViewById(R.id.cb_exit_watchdog);
        btnExitOff = findViewById(R.id.btn_exit_off);
        btnExitOn = findViewById(R.id.btn_exit_on);
        tvExitState = findViewById(R.id.tv_exit_state);

        if (savedInstanceState != null) {
            ssidPermissionRequested = savedInstanceState.getBoolean(
                    STATE_SSID_PERM_REQUESTED, false);
        }

        swAuto.setChecked(prefs.isAutoEnabled());
        cbTrusted.setChecked(prefs.isWdttTrusted());
        swExitManage.setChecked(prefs.isExitManageEnabled());
        cbExitAllowLan.setChecked(prefs.isExitNodeAllowLan());
        cbExitWatchdog.setChecked(prefs.isExitWatchdogEnabled());
        if (prefs.getExitNodeName().length() > 0) {
            etExitName.setText(prefs.getExitNodeName());
        }
        if (prefs.getExitNodeIp().length() > 0) {
            etExitIp.setText(prefs.getExitNodeIp());
        }
        etTrustedSsids.setText(joinSsids(prefs.getTrustedSsids()));

        setupListeners();

        if (prefs.isAutoEnabled() || prefs.isExitManageEnabled()) {
            startMonitor();
        }
        requestNotificationPermission();
        refreshUi();
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        outState.putBoolean(STATE_SSID_PERM_REQUESTED, ssidPermissionRequested);
    }

    @Override
    protected void onResume() {
        super.onResume();
        refreshUi();
        handler.removeCallbacks(tick);
        handler.postDelayed(tick, UI_REFRESH_MS);
    }

    @Override
    protected void onPause() {
        saveExitTexts();
        saveTrustedSsids(true);
        handler.removeCallbacks(tick);
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacks(tick);
        handler.removeCallbacks(applyTrustedSsids);
        super.onDestroy();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions,
                                           int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_POST_NOTIFICATIONS) {
            refreshUi();
            return;
        }
        if (requestCode == REQ_SSID_PERMISSION) {
            // Главное разрешение (ACCESS_FINE_LOCATION) всегда первое в массиве
            // запроса, поэтому его результат лежит в grantResults[0].
            boolean granted = grantResults.length > 0
                    && grantResults[0] == PackageManager.PERMISSION_GRANTED;
            if (granted) {
                toast(R.string.toast_ssid_permission_granted);
            } else {
                toast(R.string.toast_ssid_permission_denied);
                String permission = ssidPermission();
                if (ssidPermissionRequested
                        && !shouldShowRequestPermissionRationale(permission)) {
                    openAppDetails();
                }
            }
            refreshUi();
        }
        if (requestCode == REQ_SSID_BACKGROUND_LOCATION) {
            boolean granted = grantResults.length > 0
                    && grantResults[0] == PackageManager.PERMISSION_GRANTED;
            toast(granted
                    ? R.string.toast_background_location_granted
                    : R.string.toast_background_location_denied);
            refreshUi();
        }
    }

    private void setupListeners() {
        swAuto.setOnCheckedChangeListener(new CompoundButton.OnCheckedChangeListener() {
            @Override
            public void onCheckedChanged(CompoundButton buttonView, boolean isChecked) {
                prefs.setAutoEnabled(isChecked);
                if (isChecked) {
                    startMonitor();
                    VpnController.applyAsync(MainActivity.this, "auto_enabled", true);
                } else {
                    stopMonitor();
                }
                refreshUi();
            }
        });

        cbTrusted.setOnCheckedChangeListener(new CompoundButton.OnCheckedChangeListener() {
            @Override
            public void onCheckedChanged(CompoundButton buttonView, boolean isChecked) {
                prefs.setWdttTrusted(isChecked);
            }
        });

        swExitManage.setOnCheckedChangeListener(new CompoundButton.OnCheckedChangeListener() {
            @Override
            public void onCheckedChanged(CompoundButton buttonView, boolean isChecked) {
                prefs.setExitManageEnabled(isChecked);
                if (isChecked) {
                    startMonitor();
                    ExitNodeController.tickAsync(MainActivity.this);
                } else {
                    VpnState vpn = NetworkUtils.detectVpn(MainActivity.this);
                    boolean removedByUs = !ExitNodeController.isExitNodeActive(MainActivity.this)
                            && (prefs.getExitNodePendingOn() || prefs.getExitRetryAt() > 0L);
                    if (removedByUs && vpn.active && vpn.type == VpnType.TAILSCALE) {
                        // Управление выключают — возвращаем exit node, снятый watchdog'ом.
                        prefs.setExitNodePendingOn(false);
                        prefs.setExitRetryAt(0L);
                        ExitNodeController.applyNowAsync(MainActivity.this, true);
                    }
                    if (!prefs.isAutoEnabled()) {
                        stopMonitor();
                    }
                }
                refreshUi();
            }
        });

        etExitName.setOnFocusChangeListener(new View.OnFocusChangeListener() {
            @Override
            public void onFocusChange(View v, boolean hasFocus) {
                if (!hasFocus) {
                    saveExitTexts();
                }
            }
        });
        etExitIp.setOnFocusChangeListener(new View.OnFocusChangeListener() {
            @Override
            public void onFocusChange(View v, boolean hasFocus) {
                if (!hasFocus) {
                    saveExitTexts();
                }
            }
        });

        cbExitAllowLan.setOnCheckedChangeListener(new CompoundButton.OnCheckedChangeListener() {
            @Override
            public void onCheckedChanged(CompoundButton buttonView, boolean isChecked) {
                prefs.setExitNodeAllowLan(isChecked);
            }
        });

        cbExitWatchdog.setOnCheckedChangeListener(new CompoundButton.OnCheckedChangeListener() {
            @Override
            public void onCheckedChanged(CompoundButton buttonView, boolean isChecked) {
                prefs.setExitWatchdogEnabled(isChecked);
            }
        });

        btnExitOff.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                saveExitTexts();
                ExitNodeController.applyNowAsync(MainActivity.this, false);
            }
        });

        btnExitOn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                saveExitTexts();
                ExitNodeController.applyNowAsync(MainActivity.this, true);
            }
        });

        btnApply.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                VpnController.applyAsync(MainActivity.this, "manual", true);
            }
        });

        btnTsOn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                manualVpn(true, true);
            }
        });
        btnTsOff.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                manualVpn(true, false);
            }
        });
        btnWdttOn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                manualVpn(false, true);
            }
        });
        btnWdttOff.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                manualVpn(false, false);
            }
        });

        btnOpenTs.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                openPackage(NetworkUtils.PKG_TAILSCALE);
            }
        });
        btnOpenWdtt.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                openPackage(NetworkUtils.PKG_WDTT);
            }
        });

        btnBattery.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                openBatterySettings();
            }
        });

        etTrustedSsids.setOnFocusChangeListener(new View.OnFocusChangeListener() {
            @Override
            public void onFocusChange(View v, boolean hasFocus) {
                if (!hasFocus) {
                    saveTrustedSsids(true);
                }
            }
        });

        btnSaveSsids.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                saveTrustedSsids(true);
                toast(R.string.toast_ssid_saved);
                refreshUi();
            }
        });

        btnAddCurrent.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                addCurrentSsid();
            }
        });

        btnSsidPermission.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                if (!NetworkUtils.hasSsidPermission(MainActivity.this)) {
                    requestSsidPermission();
                    return;
                }
                if (!NetworkUtils.isLocationEnabled(MainActivity.this)) {
                    openLocationSettings();
                    return;
                }
                if (!NetworkUtils.hasBackgroundLocationPermission(MainActivity.this)) {
                    requestBackgroundLocation();
                }
            }
        });
    }

    // --- Доверенные Wi-Fi ---------------------------------------------------

    private void addCurrentSsid() {
        if (!NetworkUtils.hasSsidPermission(this)) {
            requestSsidPermission();
            return;
        }
        if (!NetworkUtils.isLocationEnabled(this)) {
            toast(R.string.toast_location_disabled);
            openLocationSettings();
            return;
        }
        String ssid = NetworkUtils.currentSsid(this);
        if (ssid == null || ssid.trim().length() == 0) {
            toast(R.string.toast_ssid_unavailable);
            return;
        }
        ssid = ssid.trim();
        boolean already = prefs.isTrustedSsid(ssid);
        prefs.addTrustedSsid(ssid);
        addSsidToEditor(ssid);
        updateTrustedCount();
        refreshUi();
        toast(already ? R.string.toast_ssid_already : R.string.toast_ssid_added);
        scheduleApplyAfterSsidsChange();
    }

    /** Добавляет сеть в поле, сохраняя уже введённые пользователем строки. */
    private void addSsidToEditor(String ssid) {
        Set<String> values = parseSsids(etTrustedSsids.getText().toString());
        values.add(ssid);
        String joined = joinSsids(values);
        etTrustedSsids.setText(joined);
        etTrustedSsids.setSelection(joined.length());
    }

    private void saveTrustedSsids(boolean force) {
        if (prefs == null || etTrustedSsids == null) {
            return;
        }
        if (!force && etTrustedSsids.hasFocus()) {
            return;
        }
        Set<String> parsed = parseSsids(etTrustedSsids.getText().toString());
        Set<String> before = prefs.getTrustedSsids();
        prefs.setTrustedSsids(parsed);
        Set<String> after = prefs.getTrustedSsids();
        if (!before.equals(after)) {
            scheduleApplyAfterSsidsChange();
        }
        updateTrustedCount(parsed.size());
        if (!etTrustedSsids.hasFocus()) {
            String joined = joinSsids(parsed);
            if (!joined.equals(etTrustedSsids.getText().toString())) {
                etTrustedSsids.setText(joined);
            }
        }
    }

    private void scheduleApplyAfterSsidsChange() {
        handler.removeCallbacks(applyTrustedSsids);
        handler.postDelayed(applyTrustedSsids, 600L);
    }

    private static Set<String> parseSsids(String raw) {
        Set<String> result = new LinkedHashSet<>();
        if (raw == null) {
            return result;
        }
        String[] parts = raw.split("[,\\n;]+");
        for (String part : parts) {
            String value = part == null ? "" : part.trim();
            if (value.length() >= 2) {
                char first = value.charAt(0);
                char last = value.charAt(value.length() - 1);
                if ((first == '"' && last == '"') || (first == '\'' && last == '\'')) {
                    value = value.substring(1, value.length() - 1).trim();
                }
            }
            if (value.length() > 0) {
                result.add(value);
            }
        }
        return result;
    }

    private static String joinSsids(Set<String> values) {
        List<String> list = new ArrayList<>();
        if (values != null) {
            for (String value : values) {
                if (value == null) {
                    continue;
                }
                String trimmed = value.trim();
                if (trimmed.length() > 0) {
                    list.add(trimmed);
                }
            }
        }
        Collections.sort(list, String.CASE_INSENSITIVE_ORDER);
        StringBuilder builder = new StringBuilder();
        for (int i = 0; i < list.size(); i++) {
            if (i > 0) {
                builder.append('\n');
            }
            builder.append(list.get(i));
        }
        return builder.toString();
    }

    private void updateTrustedCount() {
        Set<String> values = prefs.getTrustedSsids();
        updateTrustedCount(values == null ? 0 : values.size());
    }

    private void updateTrustedCount(int count) {
        if (tvSsidCount != null) {
            tvSsidCount.setText(getString(R.string.trusted_count, count));
        }
    }

    private String ssidPermission() {
        // SSID — location-sensitive поле, поэтому на всех версиях Android нужен FINE.
        return Manifest.permission.ACCESS_FINE_LOCATION;
    }

    private void requestSsidPermission() {
        ssidPermissionRequested = true;
        // FINE всегда идёт первым: его результат читается из grantResults[0].
        // COARSE запрашивается вместе с FINE: на Android 12+ запрос FINE без COARSE
        // игнорируется, на более старых версиях COARSE безвреден.
        String[] permissions = new String[]{
                Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION
        };
        try {
            requestPermissions(permissions, REQ_SSID_PERMISSION);
        } catch (Throwable t) {
            Log.e(TAG, "requestSsidPermission failed", t);
            toast(R.string.toast_ssid_permission_denied);
        }
    }

    /**
     * Доступ к местоположению «всегда»: без него SSID скрыт, пока экран приложения
     * закрыт. На Android 11+ системный диалог не предлагает «Разрешить всегда»,
     * поэтому ведём пользователя в настройки приложения; на Android 10 работает
     * обычный запрос разрешения.
     */
    private void requestBackgroundLocation() {
        if (Build.VERSION.SDK_INT >= 30) {
            Toast.makeText(this, R.string.toast_background_location_hint,
                    Toast.LENGTH_LONG).show();
            openAppDetails();
            return;
        }
        try {
            requestPermissions(
                    new String[]{Manifest.permission.ACCESS_BACKGROUND_LOCATION},
                    REQ_SSID_BACKGROUND_LOCATION);
        } catch (Throwable t) {
            Log.e(TAG, "requestBackgroundLocation failed", t);
            Toast.makeText(this, R.string.toast_background_location_hint,
                    Toast.LENGTH_LONG).show();
            openAppDetails();
        }
    }

    private void openAppDetails() {
        try {
            Intent intent = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.parse("package:" + getPackageName()));
            startActivity(intent);
        } catch (Throwable t) {
            Log.e(TAG, "Не удалось открыть настройки приложения", t);
            toast(R.string.toast_app_settings_failed);
        }
    }

    private void openLocationSettings() {
        try {
            startActivity(new Intent(Settings.ACTION_LOCATION_SOURCE_SETTINGS));
        } catch (Throwable t) {
            Log.e(TAG, "Не удалось открыть настройки геолокации", t);
            toast(R.string.toast_location_settings_failed);
        }
    }

    // --- Exit node ----------------------------------------------------------

    private void manualVpn(boolean tailscale, boolean connect) {
        String pkg = tailscale ? NetworkUtils.PKG_TAILSCALE : NetworkUtils.PKG_WDTT;
        if (!NetworkUtils.isInstalled(this, pkg)) {
            toast(R.string.toast_not_installed);
            return;
        }

        VpnState state = NetworkUtils.detectVpn(this);
        VpnType wanted = tailscale ? VpnType.TAILSCALE : VpnType.WDTT;

        if (connect) {
            if (state.active) {
                if (state.type == wanted) {
                    toast(R.string.toast_already_connected);
                } else {
                    toast(R.string.toast_other_vpn);
                }
                return;
            }
            if (tailscale) {
                VpnController.manualTailscale(this, true);
            } else {
                VpnController.manualWdtt(this, true);
            }
        } else {
            if (!state.active || state.type != wanted) {
                toast(R.string.toast_not_connected);
                return;
            }
            if (tailscale) {
                VpnController.manualTailscale(this, false);
            } else {
                VpnController.manualWdtt(this, false);
            }
        }
        refreshUi();
    }

    private void openPackage(String pkg) {
        if (!NetworkUtils.isInstalled(this, pkg)) {
            toast(R.string.toast_not_installed);
            return;
        }
        try {
            VpnController.openApp(this, pkg);
        } catch (Throwable t) {
            Log.e(TAG, "openPackage failed: " + pkg, t);
            toast(R.string.toast_open_failed);
        }
    }

    private void openBatterySettings() {
        try {
            Intent request = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
            request.setData(Uri.parse("package:" + getPackageName()));
            startActivity(request);
        } catch (Throwable t) {
            Log.d(TAG, "REQUEST_IGNORE_BATTERY_OPTIMIZATIONS недоступен: " + t);
            try {
                startActivity(new Intent(
                        Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
            } catch (Throwable t2) {
                Log.e(TAG, "Настройки батареи недоступны", t2);
                toast(R.string.toast_settings_failed);
            }
        }
    }

    /** Сохраняет текстовые поля exit node; пустые значения не затирают настройки. */
    private void saveExitTexts() {
        if (prefs == null || etExitName == null || etExitIp == null) {
            return;
        }
        String name = etExitName.getText().toString().trim();
        if (name.length() > 0) {
            prefs.setExitNodeName(name);
        }
        String ip = etExitIp.getText().toString().trim();
        if (ip.length() > 0) {
            prefs.setExitNodeIp(ip);
        }
    }

    private void startMonitor() {
        try {
            Intent intent = new Intent(this, MonitorService.class);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(intent);
            } else {
                startService(intent);
            }
        } catch (Throwable t) {
            Log.e(TAG, "startMonitor failed", t);
            toast(R.string.toast_service_failed);
        }
    }

    private void stopMonitor() {
        try {
            stopService(new Intent(this, MonitorService.class));
        } catch (Throwable t) {
            Log.e(TAG, "stopMonitor failed", t);
        }
    }

    private void requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},
                    REQ_POST_NOTIFICATIONS);
        }
    }

    // --- Обновление интерфейса ---------------------------------------------

    private void refreshUi() {
        if (prefs == null) {
            return;
        }
        NetKind kind = NetworkUtils.currentKind(this);
        VpnState vpn = NetworkUtils.detectVpn(this);
        boolean ssidPermission = NetworkUtils.hasSsidPermission(this);
        boolean locationEnabled = NetworkUtils.isLocationEnabled(this);
        String ssid = NetworkUtils.currentSsid(this);

        updateHero(kind, vpn, ssid, ssidPermission, locationEnabled);
        updateTrustedWifi(kind, ssid, ssidPermission, locationEnabled);

        if (swAuto.isChecked() != prefs.isAutoEnabled()) {
            swAuto.setChecked(prefs.isAutoEnabled());
        }
        if (cbTrusted.isChecked() != prefs.isWdttTrusted()) {
            cbTrusted.setChecked(prefs.isWdttTrusted());
        }
        if (swExitManage.isChecked() != prefs.isExitManageEnabled()) {
            swExitManage.setChecked(prefs.isExitManageEnabled());
        }
        if (cbExitAllowLan.isChecked() != prefs.isExitNodeAllowLan()) {
            cbExitAllowLan.setChecked(prefs.isExitNodeAllowLan());
        }
        if (cbExitWatchdog.isChecked() != prefs.isExitWatchdogEnabled()) {
            cbExitWatchdog.setChecked(prefs.isExitWatchdogEnabled());
        }

        boolean monitorRunning = MonitorService.isRunning();
        tvMonitorState.setText(monitorRunning
                ? R.string.monitor_running : R.string.monitor_stopped);
        tvMonitorState.setTextColor(getColor(monitorRunning
                ? R.color.colorGreen : R.color.colorMuted));

        boolean tsInstalled = NetworkUtils.isInstalled(this, NetworkUtils.PKG_TAILSCALE);
        boolean wdttInstalled = NetworkUtils.isInstalled(this, NetworkUtils.PKG_WDTT);
        boolean applying = VpnController.isApplying();

        btnTsOn.setEnabled(!applying && !vpn.active && tsInstalled);
        btnTsOff.setEnabled(!applying && vpn.active && vpn.type == VpnType.TAILSCALE);
        btnWdttOn.setEnabled(!applying && !vpn.active && wdttInstalled);
        btnWdttOff.setEnabled(!applying && vpn.active && vpn.type == VpnType.WDTT);
        btnOpenTs.setEnabled(tsInstalled);
        btnOpenWdtt.setEnabled(wdttInstalled);
        btnApply.setEnabled(!applying);
        btnApply.setText(applying ? R.string.btn_applying : R.string.btn_apply);

        boolean exitActive = ExitNodeController.isExitNodeActive(this);
        btnExitOff.setEnabled(tsInstalled);
        btnExitOn.setEnabled(tsInstalled);
        tvExitState.setText(ExitNodeController.stateLine(this));
        if (exitActive) {
            tvExitState.setTextColor(getColor(R.color.colorGreen));
        } else if (prefs.getExitNodePendingOn() || prefs.getExitRetryAt() > 0L) {
            tvExitState.setTextColor(getColor(R.color.colorAmber));
        } else {
            tvExitState.setTextColor(getColor(R.color.colorTextSecondary));
        }

        setDiag(tvDiagTailscale, tsInstalled,
                tsInstalled ? R.string.diag_installed : R.string.diag_not_installed);

        setDiag(tvDiagWdtt, wdttInstalled,
                wdttInstalled ? R.string.diag_installed : R.string.diag_not_installed);

        boolean notifications = NetworkUtils.notificationsEnabled(this);
        setDiag(tvDiagNotif, notifications,
                notifications ? R.string.diag_notif_allowed : R.string.diag_notif_denied);

        boolean batteryOptimized = NetworkUtils.isBatteryOptimized(this);
        setDiag(tvDiagBattery, !batteryOptimized,
                batteryOptimized ? R.string.diag_battery_on : R.string.diag_battery_off);

        if (!ssidPermission) {
            setDiag(tvDiagSsid, false, R.string.diag_denied);
        } else if (!locationEnabled) {
            setDiag(tvDiagSsid, false, R.string.diag_location_off);
        } else if (!NetworkUtils.hasBackgroundLocationPermission(this)) {
            setDiag(tvDiagSsid, false, R.string.diag_background_denied);
        } else {
            setDiag(tvDiagSsid, true, R.string.diag_allowed);
        }
    }

    private void updateHero(NetKind kind, VpnState vpn, String ssid, boolean ssidPermission,
                            boolean locationEnabled) {
        String network;
        if (kind == NetKind.WIFI) {
            if (!ssidPermission) {
                network = getString(R.string.network_wifi_no_access);
            } else if (!locationEnabled) {
                network = getString(R.string.network_wifi_location_off);
            } else if (ssid == null || ssid.length() == 0) {
                network = getString(R.string.network_wifi_ssid_unavailable);
            } else {
                network = getString(R.string.network_wifi_ssid,
                        NetworkUtils.kindLabel(kind), ssid);
            }
        } else {
            network = NetworkUtils.kindLabel(kind);
        }
        tvHeroNetwork.setText(network);
        tvHeroVpn.setText(NetworkUtils.vpnLabel(vpn));
        tvHeroRule.setText(StatusText.desiredRule(this));
        tvHeroLast.setText(StatusText.lastLine(this));

        if (vpn.active && (vpn.type == VpnType.TAILSCALE || vpn.type == VpnType.WDTT)) {
            tvHeroVpn.setTextColor(getColor(R.color.colorGreen));
        } else if (vpn.active) {
            tvHeroVpn.setTextColor(getColor(R.color.colorRed));
        } else {
            tvHeroVpn.setTextColor(getColor(R.color.colorTextSecondary));
        }

        String lastError = prefs.getLastError();
        String lastStatus = prefs.getLastStatus();
        boolean lastFailed = (lastError != null && lastError.length() > 0)
                || (lastStatus != null && lastStatus.length() > 0 && !prefs.isLastOk());
        tvHeroLast.setTextColor(getColor(lastFailed
                ? R.color.colorRed : R.color.colorTextSecondary));

        boolean applying = VpnController.isApplying();
        int badgeText;
        int badgeColor;
        int badgeBackground;
        if (applying) {
            badgeText = R.string.hero_badge_applying;
            badgeColor = R.color.colorAmber;
            badgeBackground = R.drawable.bg_pill_amber;
        } else if (lastFailed) {
            badgeText = R.string.hero_badge_error;
            badgeColor = R.color.colorRed;
            badgeBackground = R.drawable.bg_pill_red;
        } else if (vpn.active && vpn.type == VpnType.OTHER) {
            badgeText = R.string.hero_badge_error;
            badgeColor = R.color.colorRed;
            badgeBackground = R.drawable.bg_pill_red;
        } else if (kind == NetKind.NONE || kind == NetKind.OTHER) {
            badgeText = R.string.hero_badge_wait;
            badgeColor = R.color.colorMuted;
            badgeBackground = R.drawable.bg_pill_gray;
        } else if (ruleSatisfied(kind, vpn)) {
            badgeText = R.string.hero_badge_ok;
            badgeColor = R.color.colorGreen;
            badgeBackground = R.drawable.bg_pill_green;
        } else {
            badgeText = R.string.hero_badge_switch;
            badgeColor = R.color.colorAmber;
            badgeBackground = R.drawable.bg_pill_amber;
        }

        tvStatusBadge.setText(badgeText);
        tvStatusBadge.setTextColor(getColor(badgeColor));
        tvStatusBadge.setBackgroundResource(badgeBackground);
        viewStatusDot.setBackgroundTintList(ColorStateList.valueOf(getColor(badgeColor)));
    }

    /** Выполнено ли правило для текущей сети и активного VPN. */
    private boolean ruleSatisfied(NetKind kind, VpnState vpn) {
        boolean wifi = kind == NetKind.WIFI;
        boolean mobile = kind == NetKind.MOBILE;
        boolean tailscale = vpn.active && vpn.type == VpnType.TAILSCALE;
        boolean wdtt = vpn.active && vpn.type == VpnType.WDTT;

        if (wifi) {
            if (NetworkUtils.isTrustedWifi(this)) {
                return !vpn.active;
            }
            return tailscale;
        }
        if (mobile) {
            return wdtt;
        }
        return false;
    }

    private void updateTrustedWifi(NetKind kind, String ssid, boolean ssidPermission,
                                   boolean locationEnabled) {
        String shownSsid = (ssid == null || ssid.length() == 0)
                ? getString(R.string.value_dash) : ssid;
        tvCurrentSsid.setText(getString(R.string.label_current_ssid, shownSsid));

        int statusText;
        int statusColor;
        int statusBackground;
        if (!ssidPermission) {
            statusText = R.string.ssid_no_access;
            statusColor = R.color.colorAmber;
            statusBackground = R.drawable.bg_pill_amber;
        } else if (kind != NetKind.WIFI) {
            statusText = R.string.ssid_no_wifi;
            statusColor = R.color.colorMuted;
            statusBackground = R.drawable.bg_pill_gray;
        } else if (!locationEnabled) {
            statusText = R.string.ssid_location_off;
            statusColor = R.color.colorAmber;
            statusBackground = R.drawable.bg_pill_amber;
        } else if (ssid == null || ssid.length() == 0) {
            statusText = R.string.ssid_unavailable;
            statusColor = R.color.colorAmber;
            statusBackground = R.drawable.bg_pill_amber;
        } else if (prefs.isTrustedSsid(ssid)) {
            statusText = R.string.ssid_trusted;
            statusColor = R.color.colorGreen;
            statusBackground = R.drawable.bg_pill_green;
        } else {
            statusText = R.string.ssid_untrusted;
            statusColor = R.color.colorAmber;
            statusBackground = R.drawable.bg_pill_amber;
        }
        tvSsidStatus.setText(statusText);
        tvSsidStatus.setTextColor(getColor(statusColor));
        tvSsidStatus.setBackgroundResource(statusBackground);

        boolean backgroundLocation = NetworkUtils.hasBackgroundLocationPermission(this);
        if (!ssidPermission) {
            btnSsidPermission.setText(R.string.btn_ssid_permission);
            btnSsidPermission.setEnabled(true);
        } else if (kind == NetKind.WIFI && !locationEnabled) {
            // Без включённой геолокации Android скрывает SSID на всех версиях.
            btnSsidPermission.setText(R.string.btn_ssid_location_settings);
            btnSsidPermission.setEnabled(true);
        } else if (!backgroundLocation) {
            // «При использовании» хватает только открытому экрану: в фоне SSID маскируется.
            btnSsidPermission.setText(R.string.btn_ssid_background_location);
            btnSsidPermission.setEnabled(true);
        } else {
            btnSsidPermission.setText(R.string.btn_ssid_permission_granted);
            btnSsidPermission.setEnabled(false);
        }

        updateTrustedCount();

        // Не трогаем поле, пока пользователь его редактирует.
        if (!etTrustedSsids.hasFocus()) {
            String joined = joinSsids(prefs.getTrustedSsids());
            if (!joined.equals(etTrustedSsids.getText().toString())) {
                etTrustedSsids.setText(joined);
            }
        }
    }

    private void setDiag(TextView view, boolean good, int textRes) {
        view.setText(textRes);
        view.setTextColor(getColor(good ? R.color.colorGreen : R.color.colorRed));
    }

    private void toast(int resId) {
        Toast.makeText(this, resId, Toast.LENGTH_SHORT).show();
    }
}
