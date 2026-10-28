package com.vpnswitch;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.Editable;
import android.text.TextUtils;
import android.text.TextWatcher;
import android.util.Log;
import android.view.View;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.CompoundButton;
import android.widget.EditText;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import java.util.ArrayList;
import java.util.Collection;
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
    private static final long UI_REFRESH_MS = 5000L;
    private static final String STATE_SSID_PERM_REQUESTED = "ssid_perm_requested";
    private static final String STATE_SCROLL_Y = "scroll_y";

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
            ssidsApplyPending = false;
            // Application context: задача может пережить Activity при повороте.
            VpnController.applyAsync(getApplicationContext(), "trusted_ssids", true);
        }
    };

    /** Запланировано ли применение списка доверенных сетей. */
    private boolean ssidsApplyPending;

    private Prefs prefs;

    private ScrollView scrollRoot;
    private View bannerNotifications;
    private Button btnNotifSettings;

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

    /** Показывали ли объяснение про выключенные уведомления в этом экземпляре. */
    private boolean notificationExplanationShown;

    /** Кэш установки Tailscale для быстрого включения кнопки exit node. */
    private boolean tsInstalled;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        prefs = new Prefs(this);
        Notifications.ensureChannels(this);

        scrollRoot = findViewById(R.id.scroll_root);
        bannerNotifications = findViewById(R.id.banner_notifications);
        btnNotifSettings = findViewById(R.id.btn_notif_settings);

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

        TextView tvAppVersion = findViewById(R.id.tv_app_version);
        TextView tvFooterVersion = findViewById(R.id.tv_footer_version);
        tvAppVersion.setText(getString(R.string.app_version_format, BuildConfig.VERSION_NAME));
        tvFooterVersion.setText(getString(R.string.footer_version_format, BuildConfig.VERSION_NAME));

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
        etTrustedSsids.setText(joinTrustedEntries(prefs.getTrustedNetworks()));

        setupListeners();
        btnNotifSettings.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                if (!postNotificationsGranted()) {
                    // Причина нажатия — баннер: сразу объясняем, зачем нужно
                    // разрешение, и запрашиваем его по согласию пользователя.
                    showNotificationExplanation();
                } else {
                    openNotificationSettings();
                }
            }
        });

        if (prefs.isAutoEnabled() || prefs.isExitManageEnabled()) {
            startMonitor();
        }
        refreshUi();

        if (savedInstanceState != null) {
            final int scrollY = savedInstanceState.getInt(STATE_SCROLL_Y, 0);
            if (scrollY > 0 && scrollRoot != null) {
                scrollRoot.post(new Runnable() {
                    @Override
                    public void run() {
                        scrollRoot.scrollTo(0, scrollY);
                    }
                });
            }
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        outState.putBoolean(STATE_SSID_PERM_REQUESTED, ssidPermissionRequested);
        outState.putInt(STATE_SCROLL_Y, scrollRoot == null ? 0 : scrollRoot.getScrollY());
    }

    @Override
    protected void onResume() {
        super.onResume();
        // Пользователь мог установить/удалить Tailscale или WDTT, пока экран был
        // в фоне: кэш PackageManager сбрасываем и обновляем UI сразу.
        NetworkUtils.invalidateInstalledCache();
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
        if (ssidsApplyPending) {
            // Поворот/выход сразу после изменения списка не должен терять
            // обещанное применение правила.
            handler.removeCallbacks(applyTrustedSsids);
            ssidsApplyPending = false;
            VpnController.applyAsync(getApplicationContext(), "ssids_changed", true);
        } else {
            handler.removeCallbacks(applyTrustedSsids);
        }
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
                    maybeExplainNotificationsDisabled();
                } else if (prefs.isExitManageEnabled()) {
                    // Exit node продолжает управляться сервисом без auto.
                    Log.i(TAG, "Автопереключение выключено, сервис оставлен для exit node");
                } else {
                    // Ни auto, ни exit node: снимаем exit node, если он наш,
                    // и только затем останавливаем сервис.
                    ExitNodeController.disableManagementAsync(MainActivity.this);
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
                    maybeExplainNotificationsDisabled();
                } else {
                    // Выключение управления: снять pending/retry и, если exit node
                    // включало приложение, отправить команду снятия. Возвращать
                    // exit node, снятый watchdog'ом, больше не нужно.
                    ExitNodeController.disableManagementAsync(MainActivity.this);
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
        etExitName.addTextChangedListener(new TextWatcher() {
            @Override
            public void beforeTextChanged(CharSequence s, int start, int count, int after) {
                // не используется
            }

            @Override
            public void onTextChanged(CharSequence s, int start, int before, int count) {
                // не используется
            }

            @Override
            public void afterTextChanged(Editable s) {
                updateExitOnEnabled();
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
                // Правило не вернёт ноду на этой сети до смены сети или
                // выключения управления exit node.
                toast(R.string.toast_exit_manual_off);
            }
        });

        btnExitOn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                saveExitTexts();
                if (etExitName.getText().toString().trim().length() == 0) {
                    showExitNameRequired();
                    return;
                }
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
        NetworkUtils.WifiAccess access = NetworkUtils.currentWifiAccess(this);
        String ssid = access.ssid;
        if (ssid == null || ssid.trim().length() == 0) {
            toast(R.string.toast_ssid_unavailable);
            return;
        }
        if (access.security == Logic.WifiSecurity.OPEN) {
            // Открытая сеть не может быть доверенной: trustLevel всегда NONE.
            toast(R.string.toast_open_network_not_trusted);
            return;
        }
        if (access.security == Logic.WifiSecurity.UNKNOWN) {
            // Fail-closed: неизвестный тип защиты не даёт подтвердить доверие.
            toast(R.string.toast_security_unknown_not_trusted);
            return;
        }
        if (access.bssid == null) {
            // Fail-closed: без BSSID доверие невозможно, evil twin не отличить.
            toast(R.string.toast_bssid_unavailable);
            return;
        }
        ssid = ssid.trim();
        boolean already = prefs.trustLevel(ssid, access.bssid, access.security)
                == Logic.Trust.TRUSTED;
        prefs.addTrustedNetwork(ssid, access.bssid);
        addEntryToEditor(ssid, access.bssid);
        updateTrustedCount();
        refreshUi();
        toast(already ? R.string.toast_ssid_already : R.string.toast_ssid_added);
        scheduleApplyAfterSsidsChange();
    }

    /** Добавляет пару SSID+BSSID в поле, сохраняя уже введённые пользователем строки. */
    private void addEntryToEditor(String ssid, String bssid) {
        Set<Logic.TrustedEntry> values = parseTrustedEntries(
                etTrustedSsids.getText().toString());
        Logic.TrustedEntry entry = new Logic.TrustedEntry(ssid, bssid);
        if (entry.isBound()) {
            // Старая запись того же SSID без привязки заменяется привязанной.
            values.remove(new Logic.TrustedEntry(entry.ssid, ""));
        }
        values.add(entry);
        String joined = joinTrustedEntries(values);
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
        Set<Logic.TrustedEntry> parsed = parseTrustedEntries(
                etTrustedSsids.getText().toString());
        Set<Logic.TrustedEntry> before = prefs.getTrustedNetworks();
        prefs.setTrustedNetworks(parsed);
        Set<Logic.TrustedEntry> after = prefs.getTrustedNetworks();
        if (!before.equals(after)) {
            scheduleApplyAfterSsidsChange();
        }
        updateTrustedCount(parsed.size());
        if (!etTrustedSsids.hasFocus()) {
            String joined = joinTrustedEntries(parsed);
            if (!joined.equals(etTrustedSsids.getText().toString())) {
                etTrustedSsids.setText(joined);
            }
        }
    }

    private void scheduleApplyAfterSsidsChange() {
        handler.removeCallbacks(applyTrustedSsids);
        ssidsApplyPending = true;
        handler.postDelayed(applyTrustedSsids, 600L);
    }

    /**
     * Записи доверенных сетей: строго по одной в строке. Запятая и точка с
     * запятой — допустимые символы SSID, поэтому разделителями не считаются.
     */
    private static Set<Logic.TrustedEntry> parseTrustedEntries(String raw) {
        return new LinkedHashSet<>(Logic.parseTrustedEntries(raw));
    }

    private static String joinTrustedEntries(Collection<Logic.TrustedEntry> entries) {
        List<String> list = new ArrayList<>();
        if (entries != null) {
            for (Logic.TrustedEntry entry : entries) {
                if (entry == null) {
                    continue;
                }
                String display = Logic.displayTrustedEntry(entry.ssid, entry.bssid);
                if (display.length() > 0) {
                    list.add(display);
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
        Set<Logic.TrustedEntry> values = prefs.getTrustedNetworks();
        updateTrustedCount(values == null ? 0 : values.size());
    }

    private void updateTrustedCount(int count) {
        if (tvSsidCount != null) {
            setTextIfChanged(tvSsidCount, getString(R.string.trusted_count, count));
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

        boolean started;
        if (connect) {
            if (state.active) {
                if (state.type == wanted) {
                    toast(R.string.toast_already_connected);
                } else {
                    toast(R.string.toast_other_vpn);
                }
                return;
            }
            started = tailscale
                    ? VpnController.manualTailscale(this, true)
                    : VpnController.manualWdtt(this, true);
        } else {
            if (!state.active || state.type != wanted) {
                toast(R.string.toast_not_connected);
                return;
            }
            started = tailscale
                    ? VpnController.manualTailscale(this, false)
                    : VpnController.manualWdtt(this, false);
        }
        if (!started) {
            // Контроллер занят apply или другим ручным действием: вторую команду
            // не отправляем, чтобы двойной тап не переключил VPN обратно.
            toast(R.string.toast_operation_busy);
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

    /**
     * Сохраняет текстовые поля exit node. Пишутся только реально изменённые
     * значения, включая пустые: так очистка поля действительно сохраняется,
     * а onPause не затирает ввод. Пустое имя при включённом управлении —
     * ошибка валидации, но данные всё равно очищаются.
     */
    private void saveExitTexts() {
        if (prefs == null || etExitName == null || etExitIp == null) {
            return;
        }
        String name = etExitName.getText().toString().trim();
        String ip = etExitIp.getText().toString().trim();
        boolean nameChanged = !name.equals(prefs.getExitNodeName());
        boolean ipChanged = !ip.equals(prefs.getExitNodeIp());
        if (nameChanged) {
            prefs.setExitNodeName(name);
        }
        if (ipChanged) {
            prefs.setExitNodeIp(ip);
        }
        if (nameChanged && name.length() == 0 && prefs.isExitManageEnabled()) {
            showExitNameRequired();
        }
        updateExitOnEnabled();
    }

    /** Имя exit node обязательно только для включения; IP можно оставить пустым. */
    private void updateExitOnEnabled() {
        if (btnExitOn == null || etExitName == null) {
            return;
        }
        boolean hasName = etExitName.getText().toString().trim().length() > 0;
        // При выключенном управлении tick не работает: отправленная вручную
        // команда осталась бы без подтверждения и присмотра.
        btnExitOn.setEnabled(tsInstalled && hasName && prefs.isExitManageEnabled());
    }

    private void showExitNameRequired() {
        toast(R.string.toast_exit_name_required);
        if (tvExitState != null) {
            tvExitState.setText(R.string.exit_state_name_required);
            tvExitState.setTextColor(getColor(R.color.colorAmber));
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

    /** Снимок состояния экрана: все дорогие проверки выполняются один раз. */
    private static final class UiSnapshot {
        final NetKind kind;
        final VpnState vpn;
        final NetworkUtils.WifiAccess access;
        final boolean ssidPermission;
        final boolean locationEnabled;
        final boolean backgroundLocation;
        final boolean notificationsEnabled;
        final boolean tsInstalled;
        final boolean wdttInstalled;
        final boolean exitActive;
        final boolean monitorRunning;
        final boolean applying;
        final boolean batteryOptimized;

        UiSnapshot(NetKind kind, VpnState vpn, NetworkUtils.WifiAccess access,
                   boolean ssidPermission, boolean locationEnabled,
                   boolean backgroundLocation, boolean notificationsEnabled,
                   boolean tsInstalled, boolean wdttInstalled, boolean exitActive,
                   boolean monitorRunning, boolean applying, boolean batteryOptimized) {
            this.kind = kind;
            this.vpn = vpn;
            this.access = access;
            this.ssidPermission = ssidPermission;
            this.locationEnabled = locationEnabled;
            this.backgroundLocation = backgroundLocation;
            this.notificationsEnabled = notificationsEnabled;
            this.tsInstalled = tsInstalled;
            this.wdttInstalled = wdttInstalled;
            this.exitActive = exitActive;
            this.monitorRunning = monitorRunning;
            this.applying = applying;
            this.batteryOptimized = batteryOptimized;
        }
    }

    /** Один проход по сети, пакетам и разрешениям вместо повторных сканов. */
    private UiSnapshot buildSnapshot() {
        NetKind kind = NetworkUtils.currentKind(this);
        VpnState vpn = NetworkUtils.detectVpn(this);
        NetworkUtils.WifiAccess access = kind == NetKind.WIFI
                ? NetworkUtils.currentWifiAccess(this, kind) : NetworkUtils.WifiAccess.none();
        boolean ssidPermission = NetworkUtils.hasSsidPermission(this);
        boolean locationEnabled = NetworkUtils.isLocationEnabled(this);
        boolean backgroundLocation = NetworkUtils.hasBackgroundLocationPermission(this);
        boolean notificationsEnabled = postNotificationsGranted()
                && Notifications.alertsEnabled(this);
        boolean tsInstalled = NetworkUtils.isInstalled(this, NetworkUtils.PKG_TAILSCALE);
        boolean wdttInstalled = NetworkUtils.isInstalled(this, NetworkUtils.PKG_WDTT);
        boolean exitActive = tsInstalled && vpn.active && vpn.type == VpnType.TAILSCALE
                && ExitNodeController.isExitNodeActive(this);
        return new UiSnapshot(kind, vpn, access, ssidPermission, locationEnabled,
                backgroundLocation, notificationsEnabled, tsInstalled, wdttInstalled,
                exitActive, MonitorService.isRunning(), VpnController.isApplying(),
                NetworkUtils.isBatteryOptimized(this));
    }

    private void refreshUi() {
        if (prefs == null) {
            return;
        }
        UiSnapshot snapshot = buildSnapshot();

        updateHero(snapshot);
        updateTrustedWifi(snapshot);
        updateNotificationBanner(snapshot);

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

        setTextIfChanged(tvMonitorState, snapshot.monitorRunning
                ? R.string.monitor_running : R.string.monitor_stopped);
        setTextColorIfChanged(tvMonitorState, snapshot.monitorRunning
                ? R.color.colorGreen : R.color.colorMuted);

        this.tsInstalled = snapshot.tsInstalled;

        btnTsOn.setEnabled(!snapshot.applying && !snapshot.vpn.active
                && snapshot.tsInstalled);
        btnTsOff.setEnabled(!snapshot.applying && snapshot.vpn.active
                && snapshot.vpn.type == VpnType.TAILSCALE);
        btnWdttOn.setEnabled(!snapshot.applying && !snapshot.vpn.active
                && snapshot.wdttInstalled);
        btnWdttOff.setEnabled(!snapshot.applying && snapshot.vpn.active
                && snapshot.vpn.type == VpnType.WDTT);
        btnOpenTs.setEnabled(snapshot.tsInstalled);
        btnOpenWdtt.setEnabled(snapshot.wdttInstalled);
        btnApply.setEnabled(!snapshot.applying);
        setTextIfChanged(btnApply, snapshot.applying
                ? R.string.btn_applying : R.string.btn_apply);

        btnExitOff.setEnabled(snapshot.tsInstalled && prefs.isExitManageEnabled());
        updateExitOnEnabled();
        boolean manualOff = Logic.manualOffApplies(prefs.getExitManualOffKey(),
                snapshot.kind,
                snapshot.access == null ? null : snapshot.access.ssid);
        setTextIfChanged(tvExitState, StatusText.exitStateLine(this, prefs,
                snapshot.tsInstalled,
                snapshot.vpn.active && snapshot.vpn.type == VpnType.TAILSCALE,
                snapshot.exitActive, manualOff));
        if (snapshot.exitActive) {
            setTextColorIfChanged(tvExitState, R.color.colorGreen);
        } else if (manualOff || prefs.isExitPendingOff()
                || prefs.getExitNodePendingOn() || prefs.getExitRetryAt() > 0L
                || prefs.getExitConfirmAt() > 0L) {
            setTextColorIfChanged(tvExitState, R.color.colorAmber);
        } else {
            setTextColorIfChanged(tvExitState, R.color.colorTextSecondary);
        }

        setDiag(tvDiagTailscale, snapshot.tsInstalled,
                snapshot.tsInstalled ? R.string.diag_installed : R.string.diag_not_installed);

        setDiag(tvDiagWdtt, snapshot.wdttInstalled,
                snapshot.wdttInstalled ? R.string.diag_installed : R.string.diag_not_installed);

        setDiag(tvDiagNotif, snapshot.notificationsEnabled,
                snapshot.notificationsEnabled
                        ? R.string.diag_notif_allowed : R.string.diag_notif_denied);

        setDiag(tvDiagBattery, !snapshot.batteryOptimized,
                snapshot.batteryOptimized
                        ? R.string.diag_battery_on : R.string.diag_battery_off);

        if (!snapshot.ssidPermission) {
            setDiag(tvDiagSsid, false, R.string.diag_denied);
        } else if (!snapshot.locationEnabled) {
            setDiag(tvDiagSsid, false, R.string.diag_location_off);
        } else if (!snapshot.backgroundLocation) {
            setDiag(tvDiagSsid, false, R.string.diag_background_denied);
        } else {
            setDiag(tvDiagSsid, true, R.string.diag_allowed);
        }
    }

    private void updateNotificationBanner(UiSnapshot snapshot) {
        if (bannerNotifications == null) {
            return;
        }
        int visibility = snapshot.notificationsEnabled ? View.GONE : View.VISIBLE;
        if (bannerNotifications.getVisibility() != visibility) {
            bannerNotifications.setVisibility(visibility);
        }
    }

    private void updateHero(UiSnapshot snapshot) {
        NetKind kind = snapshot.kind;
        VpnState vpn = snapshot.vpn;
        NetworkUtils.WifiAccess access = snapshot.access;
        String ssid = access == null ? null : access.ssid;
        String network;
        if (kind == NetKind.WIFI) {
            if (!snapshot.ssidPermission) {
                network = getString(R.string.network_wifi_no_access);
            } else if (!snapshot.locationEnabled) {
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
        setTextIfChanged(tvHeroNetwork, network);
        setTextIfChanged(tvHeroVpn, NetworkUtils.vpnLabel(vpn));
        setTextIfChanged(tvHeroRule, StatusText.desiredRule(this, kind, access));
        setTextIfChanged(tvHeroLast, StatusText.lastLine(this));

        if (vpn.active && (vpn.type == VpnType.TAILSCALE || vpn.type == VpnType.WDTT)) {
            setTextColorIfChanged(tvHeroVpn, R.color.colorGreen);
        } else if (vpn.active) {
            setTextColorIfChanged(tvHeroVpn, R.color.colorRed);
        } else {
            setTextColorIfChanged(tvHeroVpn, R.color.colorTextSecondary);
        }

        String lastError = prefs.getLastError();
        String lastStatus = prefs.getLastStatus();
        boolean lastFailed = (lastError != null && lastError.length() > 0)
                || (lastStatus != null && lastStatus.length() > 0 && !prefs.isLastOk());
        setTextColorIfChanged(tvHeroLast, lastFailed
                ? R.color.colorRed : R.color.colorTextSecondary);

        int badgeText;
        int badgeColor;
        int badgeBackground;
        if (snapshot.applying) {
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
        } else if (ruleSatisfied(kind, vpn, access, snapshot.exitActive)) {
            badgeText = R.string.hero_badge_ok;
            badgeColor = R.color.colorGreen;
            badgeBackground = R.drawable.bg_pill_green;
        } else {
            badgeText = R.string.hero_badge_switch;
            badgeColor = R.color.colorAmber;
            badgeBackground = R.drawable.bg_pill_amber;
        }

        setPill(tvStatusBadge, badgeText, badgeColor, badgeBackground);
        setBackgroundTintIfChanged(viewStatusDot, getColor(badgeColor));
    }

    /** Выполнено ли правило для текущей сети и активного VPN. */
    private boolean ruleSatisfied(NetKind kind, VpnState vpn,
                                  NetworkUtils.WifiAccess access, boolean exitActive) {
        boolean trusted = kind == NetKind.WIFI && access != null
                && access.trust == Logic.Trust.TRUSTED;
        // На незнакомой Wi-Fi при управлении exit node одного Tailscale мало:
        // pending/confirm не считаются выполнением правила.
        boolean exitRequired = kind == NetKind.WIFI && !trusted
                && prefs.isExitManageEnabled();
        return Logic.ruleSatisfied(kind, vpn.active, vpn.type, trusted,
                exitRequired, exitActive);
    }

    private void updateTrustedWifi(UiSnapshot snapshot) {
        NetKind kind = snapshot.kind;
        NetworkUtils.WifiAccess access = snapshot.access;
        boolean ssidPermission = snapshot.ssidPermission;
        boolean locationEnabled = snapshot.locationEnabled;
        String ssid = access == null ? null : access.ssid;
        String shownSsid = (ssid == null || ssid.length() == 0)
                ? getString(R.string.value_dash) : ssid;
        setTextIfChanged(tvCurrentSsid, getString(R.string.label_current_ssid, shownSsid));

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
        } else if (access == null || access.bssid == null) {
            // Fail-closed: без BSSID сеть не может быть доверенной.
            statusText = R.string.ssid_bssid_unavailable;
            statusColor = R.color.colorAmber;
            statusBackground = R.drawable.bg_pill_amber;
        } else if (access.security == Logic.WifiSecurity.OPEN) {
            statusText = R.string.ssid_open_network;
            statusColor = R.color.colorAmber;
            statusBackground = R.drawable.bg_pill_amber;
        } else if (access.security == Logic.WifiSecurity.UNKNOWN) {
            // Неизвестный тип защиты: сеть не доверяем, VPN не отключаем.
            statusText = R.string.ssid_security_unknown;
            statusColor = R.color.colorAmber;
            statusBackground = R.drawable.bg_pill_amber;
        } else if (access.trust == Logic.Trust.TRUSTED) {
            statusText = R.string.ssid_trusted;
            statusColor = R.color.colorGreen;
            statusBackground = R.drawable.bg_pill_green;
        } else if (access.trust == Logic.Trust.UNBOUND) {
            statusText = R.string.ssid_trust_unbound;
            statusColor = R.color.colorAmber;
            statusBackground = R.drawable.bg_pill_amber;
        } else if (access.trust == Logic.Trust.MISMATCH) {
            statusText = R.string.ssid_trust_mismatch;
            statusColor = R.color.colorRed;
            statusBackground = R.drawable.bg_pill_red;
        } else {
            statusText = R.string.ssid_untrusted;
            statusColor = R.color.colorAmber;
            statusBackground = R.drawable.bg_pill_amber;
        }
        setPill(tvSsidStatus, statusText, statusColor, statusBackground);

        boolean backgroundLocation = snapshot.backgroundLocation;
        if (!ssidPermission) {
            setTextIfChanged(btnSsidPermission, R.string.btn_ssid_permission);
            btnSsidPermission.setEnabled(true);
        } else if (kind == NetKind.WIFI && !locationEnabled) {
            // Без включённой геолокации Android скрывает SSID на всех версиях.
            setTextIfChanged(btnSsidPermission, R.string.btn_ssid_location_settings);
            btnSsidPermission.setEnabled(true);
        } else if (!backgroundLocation) {
            // «При использовании» хватает только открытому экрану: в фоне SSID маскируется.
            setTextIfChanged(btnSsidPermission, R.string.btn_ssid_background_location);
            btnSsidPermission.setEnabled(true);
        } else {
            setTextIfChanged(btnSsidPermission, R.string.btn_ssid_permission_granted);
            btnSsidPermission.setEnabled(false);
        }

        updateTrustedCount();

        // Не трогаем поле, пока пользователь его редактирует.
        if (!etTrustedSsids.hasFocus()) {
            String joined = joinTrustedEntries(prefs.getTrustedNetworks());
            if (!joined.equals(etTrustedSsids.getText().toString())) {
                etTrustedSsids.setText(joined);
            }
        }
    }

    private void setDiag(TextView view, boolean good, int textRes) {
        setTextIfChanged(view, textRes);
        setTextColorIfChanged(view, good ? R.color.colorGreen : R.color.colorRed);
    }

    /** Не перерисовывает TextView, если текст не изменился. */
    private void setTextIfChanged(TextView view, CharSequence text) {
        if (view == null) {
            return;
        }
        if (!TextUtils.equals(view.getText(), text)) {
            view.setText(text);
        }
    }

    private void setTextIfChanged(TextView view, int textRes) {
        setTextIfChanged(view, getString(textRes));
    }

    private void setTextColorIfChanged(TextView view, int colorRes) {
        if (view == null) {
            return;
        }
        int color = getColor(colorRes);
        if (view.getCurrentTextColor() != color) {
            view.setTextColor(color);
        }
    }

    /** Текст, цвет и фон pill-статуса меняются только при реальном отличии. */
    private void setPill(TextView view, int textRes, int colorRes, int backgroundRes) {
        setTextIfChanged(view, textRes);
        setTextColorIfChanged(view, colorRes);
        Object tag = view.getTag();
        if (!(tag instanceof Integer) || (Integer) tag != backgroundRes) {
            view.setBackgroundResource(backgroundRes);
            view.setTag(backgroundRes);
        }
    }

    private void setBackgroundTintIfChanged(View view, int color) {
        if (view == null) {
            return;
        }
        ColorStateList current = view.getBackgroundTintList();
        ColorStateList wanted = ColorStateList.valueOf(color);
        if (current == null || !current.equals(wanted)) {
            view.setBackgroundTintList(wanted);
        }
    }

    // --- Уведомления --------------------------------------------------------

    private boolean postNotificationsGranted() {
        if (Build.VERSION.SDK_INT < 33) {
            return true;
        }
        try {
            return checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                    == PackageManager.PERMISSION_GRANTED;
        } catch (Throwable t) {
            return false;
        }
    }

    private void openNotificationSettings() {
        try {
            Intent intent = new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS);
            intent.putExtra(Settings.EXTRA_APP_PACKAGE, getPackageName());
            startActivity(intent);
        } catch (Throwable t) {
            Log.e(TAG, "Не удалось открыть настройки уведомлений", t);
            openAppDetails();
        }
    }

    /** Объясняет, почему при включении автопереключения важны уведомления. */
    private void maybeExplainNotificationsDisabled() {
        if (notificationExplanationShown
                || (postNotificationsGranted() && NetworkUtils.notificationsEnabled(this))) {
            return;
        }
        notificationExplanationShown = true;
        showNotificationExplanation();
    }

    /**
     * Диалог-объяснение с запросом разрешения по согласию. Показывается без
     * guard'а: вызывается и из баннера, и из {@link #maybeExplainNotificationsDisabled()}.
     */
    private void showNotificationExplanation() {
        try {
            new AlertDialog.Builder(this)
                    .setTitle(R.string.notif_needed_title)
                    .setMessage(R.string.notif_needed_message)
                    .setPositiveButton(R.string.notif_needed_allow,
                            new DialogInterface.OnClickListener() {
                                @Override
                                public void onClick(DialogInterface dialog, int which) {
                                    if (!postNotificationsGranted()) {
                                        requestNotificationPermission();
                                    } else {
                                        openNotificationSettings();
                                    }
                                }
                            })
                    .setNegativeButton(R.string.notif_needed_later, null)
                    .show();
        } catch (Throwable t) {
            Log.e(TAG, "showNotificationExplanation failed", t);
        }
    }

    private void toast(int resId) {
        Toast.makeText(this, resId, Toast.LENGTH_SHORT).show();
    }
}
