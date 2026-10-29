using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text.Json;
using CommunityToolkit.Mvvm.Input;
using H.NotifyIcon;
using H.NotifyIcon.Core;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using PiMusic.Data;
using PiMusic.Player;

namespace PiMusic.Services;

/**
 * Значок в области уведомлений: левый клик — показать/спрятать окно, правый — системное меню Win32
 * (открыть, воспроизведение, следующий/предыдущий, выход). Закрытие окна крестиком при CloseToTray прячет окно,
 * уведомления трея — подсказка про трей (один раз) и смена трека (если включено и окно не на виду).
 */
public static class TrayService
{
    private static Window? _window;
    private static TaskbarIcon? _icon;
    private static MenuFlyoutItem? _playItem;
    private static bool _active = true;
    private static bool _exiting;
    private static long _notifiedId;

    private static string StatePath => Path.Combine(AppSettings.Dir, "tray.json");

    /** состояние значка: подсказка «играет в трее» уже показывалась */
    private sealed class TrayState { public bool CloseHintShown { get; set; } }

    /** Вызвать один раз после создания главного окна (из потока интерфейса, после PlayerService.Init). */
    public static void Attach(Window window)
    {
        if (_window is not null) return;
        _window = window;
        var ui = window.DispatcherQueue;

        _playItem = new MenuFlyoutItem { Text = "Воспроизвести", Command = Cmd(() => PlayerService.Instance.TogglePlay()) };
        var menu = new MenuFlyout();
        menu.Items.Add(new MenuFlyoutItem { Text = "Открыть PiMusic", Command = Cmd(ShowWindow) });
        menu.Items.Add(_playItem);
        menu.Items.Add(new MenuFlyoutItem { Text = "Следующий", Command = Cmd(() => PlayerService.Instance.Next()) });
        menu.Items.Add(new MenuFlyoutItem { Text = "Предыдущий", Command = Cmd(() => PlayerService.Instance.Previous()) });
        menu.Items.Add(new MenuFlyoutSeparator());
        menu.Items.Add(new MenuFlyoutItem { Text = "Выход", Command = Cmd(ExitApp) });

        _icon = new TaskbarIcon
        {
            ToolTipText = "PiMusic",
            ContextMenuMode = ContextMenuMode.PopupMenu,
            ContextFlyout = menu,
            NoLeftClickDelay = true,
            LeftClickCommand = Cmd(ToggleWindow),
        };
        try { _icon.Icon = new System.Drawing.Icon(Path.Combine(AppContext.BaseDirectory, "Assets", "icon.ico")); }
        catch (Exception) { /* без значка трей всё равно работает */ }
        _icon.ForceCreate(false);

        window.Activated += (_, e) => _active = e.WindowActivationState != WindowActivationState.Deactivated;
        window.AppWindow.Closing += OnClosing;
        PlayerService.Instance.PropertyChanged += OnPlayerChanged;
        Update();

        // команды меню и клика приходят из окна сообщений значка — выполняем в потоке интерфейса
        // очередь потока закрыта (приложение завершается) — команду не выполняем
        RelayCommand Cmd(Action a) => new(() => ui.TryEnqueue(() => a()));
    }

    /** Выход из приложения: сохранить очередь, убрать значок, закрыть окна. */
    public static void ExitApp()
    {
        if (_exiting) return;
        _exiting = true;
        PlayerService.Instance.Shutdown();
        _icon?.Dispose();
        _icon = null;
        Application.Current.Exit();
    }

    /** Показать окно: восстановить из свёрнутого и вывести на передний план. */
    public static void ShowWindow()
    {
        if (_window is null) return;
        var aw = _window.AppWindow;
        aw.Show();
        if (aw.Presenter is OverlappedPresenter { State: OverlappedPresenterState.Minimized } p) p.Restore();
        _window.Activate();
        SetForegroundWindow(WinRT.Interop.WindowNative.GetWindowHandle(_window));
    }

    private static void ToggleWindow()
    {
        if (_window is null) return;
        var aw = _window.AppWindow;
        var minimized = aw.Presenter is OverlappedPresenter { State: OverlappedPresenterState.Minimized };
        if (aw.IsVisible && !minimized) aw.Hide();
        else ShowWindow();
    }

    private static void OnClosing(AppWindow sender, AppWindowClosingEventArgs args)
    {
        if (_exiting) return;
        args.Cancel = true;
        if (!AppSettings.Current.CloseToTray) { ExitApp(); return; }
        sender.Hide();
        var st = LoadState();
        if (st.CloseHintShown) return;
        Notify("PiMusic", "PiMusic продолжает играть в трее");
        st.CloseHintShown = true;
        SaveState(st);
    }

    private static void OnPlayerChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName is not (nameof(PlayerService.Current) or nameof(PlayerService.IsPlaying))) return;
        Update();
        // уведомление о смене трека — когда трек реально играет и окно не на виду (восстановленная очередь молчит)
        var p = PlayerService.Instance;
        if (!AppSettings.Current.TrackNotifications || !p.IsPlaying || p.Current is not { } t || t.Id == _notifiedId) return;
        _notifiedId = t.Id;
        if (_window is not null && _window.AppWindow.IsVisible && _active) return;
        Notify(t.Title, t.Artist);
    }

    private static void Update()
    {
        var p = PlayerService.Instance;
        if (_playItem is not null) _playItem.Text = p.IsPlaying ? "Пауза" : "Воспроизвести";
        if (_icon is null) return;
        var tip = p.Current is { } t ? $"{t.Artist} — {t.Title}" : "PiMusic";
        // подсказка значка ограничена 127 символами
        _icon.ToolTipText = tip.Length > 120 ? tip[..119] + "…" : tip;
    }

    private static void Notify(string title, string message)
    {
        try
        {
            _icon?.ShowNotification(title, message, NotificationIcon.None, sound: false);
        }
        catch (Exception) { /* уведомления отключены в системе */ }
    }

    private static TrayState LoadState()
    {
        try
        {
            if (File.Exists(StatePath))
                return JsonSerializer.Deserialize<TrayState>(File.ReadAllText(StatePath), Json.Options) ?? new TrayState();
        }
        catch (Exception) { }
        return new TrayState();
    }

    private static void SaveState(TrayState st)
    {
        try
        {
            Directory.CreateDirectory(AppSettings.Dir);
            var tmp = StatePath + ".tmp";
            File.WriteAllText(tmp, JsonSerializer.Serialize(st, Json.Options));
            File.Move(tmp, StatePath, overwrite: true);
        }
        catch (Exception) { }
    }

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);
}
