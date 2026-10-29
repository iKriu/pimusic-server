using Microsoft.UI;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Navigation;
using PiMusic.Data;
using PiMusic.Player;
using PiMusic.Views;
using Windows.Graphics;

namespace PiMusic;

public sealed partial class MainWindow : Window
{
    private static readonly Dictionary<string, Type> Pages = new()
    {
        ["library"] = typeof(LibraryPage),
        ["playlists"] = typeof(PlaylistsPage),
        ["artists"] = typeof(ArtistsPage),
        ["albums"] = typeof(AlbumsPage),
        ["offline"] = typeof(OfflinePage),
        ["wave"] = typeof(WavePage),
        ["radio"] = typeof(RadioPage),
        ["search"] = typeof(SearchPage),
        ["downloads"] = typeof(DownloadsPage),
        ["now"] = typeof(NowPlayingPage),
        ["settings"] = typeof(SettingsPage),
    };

    private readonly DispatcherTimer _toastTimer = new() { Interval = TimeSpan.FromSeconds(5) };
    private bool _syncingNav;

    public MainWindow()
    {
        InitializeComponent();
        ExtendsContentIntoTitleBar = true;
        SetTitleBar(AppTitleBar);
        AppWindow.SetIcon(Path.Combine(AppContext.BaseDirectory, "Assets", "icon.ico"));
        AppWindow.TitleBar.PreferredHeightOption = TitleBarHeightOption.Tall;
        if (AppWindow.Presenter is OverlappedPresenter op)
        {
            op.PreferredMinimumWidth = 560;
            op.PreferredMinimumHeight = 520;
        }
        RestorePlacement();
        ApplyTheme(AppSettings.Current.Theme);

        _toastTimer.Tick += (_, _) => { _toastTimer.Stop(); ToastBar.IsOpen = false; };
        AppWindow.Changed += (_, a) => { if (a.DidPositionChange || a.DidSizeChange || a.DidPresenterChange) SavePlacementSoon(); };
        Api.Instance.AuthLost += () => DispatcherQueue.TryEnqueue(ShowLogin);
    }

    // ---------------------------------------------------------------- вход / основной экран

    public void ShowLogin()
    {
        PlayerService.Instance.ClearQueue();
        ResetPages();
        LoginFrame.Navigate(typeof(LoginPage));
        LoginFrame.Visibility = Visibility.Visible;
        Nav.Visibility = Visibility.Collapsed;
        Bar.Visibility = Visibility.Collapsed;
        SearchBox.Visibility = Visibility.Collapsed;
        AppTitleBar.IsBackButtonVisible = false;
        ActivityWatcher.Stop();
    }

    public void ShowMain()
    {
        LoginFrame.Visibility = Visibility.Collapsed;
        LoginFrame.Content = null;
        Nav.Visibility = Visibility.Visible;
        Bar.Visibility = Visibility.Visible;
        SearchBox.Visibility = Visibility.Visible;
        AppTitleBar.IsBackButtonVisible = true;
        ResetPages();
        ContentFrame.Navigate(typeof(LibraryPage));
        ActivityWatcher.Start();
        StartSettingsSync();
    }

    private DispatcherQueueTimerHolder? _syncTimer;

    /** Настройки между устройствами (эквалайзер): сразу после входа и раз в минуту, пока окно видно. */
    private void StartSettingsSync()
    {
        SettingsStore.Instance.SetLogin(AppSettings.Current.Login);
        _ = SyncSettings();
        _syncTimer ??= new DispatcherQueueTimerHolder(DispatcherQueue, TimeSpan.FromMinutes(1), () =>
        {
            if (AppWindow.IsVisible && Api.Instance.LoggedIn) _ = SyncSettings();
            _syncTimer?.Restart();
        });
        _syncTimer.Restart();
    }

    private static async Task SyncSettings()
    {
        var synced = await SettingsStore.Instance.Sync();
        EqController.Instance.Refresh(synced);
    }

    // ---------------------------------------------------------------- навигация

    /** Забыть открытые страницы и их кэш (другой аккаунт или сервер — данные прежнего не показывать). */
    private void ResetPages()
    {
        var size = ContentFrame.CacheSize;
        ContentFrame.CacheSize = 0;
        ContentFrame.Content = null;
        ContentFrame.BackStack.Clear();
        ContentFrame.ForwardStack.Clear();
        ContentFrame.CacheSize = size;
    }

    public void Navigate(Type page, object? parameter = null)
    {
        if (ContentFrame.CurrentSourcePageType == page && parameter is null) return;
        ContentFrame.Navigate(page, parameter);
    }

    private void Nav_SelectionChanged(NavigationView sender, NavigationViewSelectionChangedEventArgs args)
    {
        if (_syncingNav) return;
        var tag = args.IsSettingsSelected ? "settings" : (args.SelectedItem as NavigationViewItem)?.Tag as string;
        if (tag is not null && Pages.TryGetValue(tag, out var page)) Navigate(page);
    }

    private void ContentFrame_Navigated(object sender, NavigationEventArgs e)
    {
        AppTitleBar.IsBackButtonEnabled = ContentFrame.CanGoBack;
        // Подсветить пункт меню текущей страницы (для внутренних страниц — снять выделение).
        _syncingNav = true;
        try
        {
            var tag = Pages.FirstOrDefault(p => p.Value == e.SourcePageType).Key;
            if (tag == "settings") Nav.SelectedItem = Nav.SettingsItem;
            else
                Nav.SelectedItem = Nav.MenuItems.Concat(Nav.FooterMenuItems).OfType<NavigationViewItem>()
                    .FirstOrDefault(i => (string?)i.Tag == tag);
        }
        finally { _syncingNav = false; }
    }

    private void TitleBar_BackRequested(TitleBar sender, object args)
    {
        if (ContentFrame.CanGoBack) ContentFrame.GoBack();
    }

    private void SearchBox_QuerySubmitted(AutoSuggestBox sender, AutoSuggestBoxQuerySubmittedEventArgs args)
    {
        var q = args.QueryText.Trim();
        // вставили ссылку-приглашение в плейлист — вступление, а не поиск
        if (q.Contains("#join=", StringComparison.Ordinal) || q.Contains("start=pl_", StringComparison.Ordinal))
        {
            sender.Text = "";
            _ = PlaylistDialogs.Join(Content.XamlRoot, q);
            return;
        }
        if (q.Length > 0) ContentFrame.Navigate(typeof(SearchPage), q);
    }

    /** Значок на пункте «Загрузки»: сколько сейчас качается (0 — скрыть). */
    public void SetDownloadsBadge(int count)
    {
        DownloadsBadge.Value = count;
        DownloadsBadge.Visibility = count > 0 ? Visibility.Visible : Visibility.Collapsed;
    }

    // ---------------------------------------------------------------- сообщения

    public void Toast(string text, InfoBarSeverity severity) => Toast(text, severity, null, null);

    /** Сообщение с кнопкой действия (например, «Открыть» созданный плейлист); с кнопкой висит дольше. */
    public void Toast(string text, InfoBarSeverity severity, string? actionText, Action? action)
    {
        ToastBar.Severity = severity;
        ToastBar.Message = text;
        if (actionText is not null && action is not null)
        {
            var b = new Button { Content = actionText };
            b.Click += (_, _) => { ToastBar.IsOpen = false; action(); };
            ToastBar.ActionButton = b;
        }
        else ToastBar.ActionButton = null;
        ToastBar.IsOpen = true;
        _toastTimer.Stop();
        _toastTimer.Interval = TimeSpan.FromSeconds(action is null ? 5 : 8);
        _toastTimer.Start();
    }

    // ---------------------------------------------------------------- тема и положение окна

    public void ApplyTheme(string theme)
    {
        Root.RequestedTheme = theme switch { "light" => ElementTheme.Light, "dark" => ElementTheme.Dark, _ => ElementTheme.Default };
        AppWindow.TitleBar.PreferredTheme = theme switch
        {
            "light" => TitleBarTheme.Light,
            "dark" => TitleBarTheme.Dark,
            _ => TitleBarTheme.UseDefaultAppMode,
        };
    }

    private void RestorePlacement()
    {
        var s = AppSettings.Current;
        if (s.WindowRect is [var x, var y, var w, var h] && w >= 560 && h >= 520)
        {
            var area = DisplayArea.GetFromRect(new RectInt32(x, y, w, h), DisplayAreaFallback.Nearest);
            var wa = area.WorkArea;
            x = Math.Clamp(x, wa.X, Math.Max(wa.X, wa.X + wa.Width - w));
            y = Math.Clamp(y, wa.Y, Math.Max(wa.Y, wa.Y + wa.Height - h));
            AppWindow.MoveAndResize(new RectInt32(x, y, Math.Min(w, wa.Width), Math.Min(h, wa.Height)));
        }
        else
        {
            var scale = Content?.XamlRoot?.RasterizationScale ?? 1.0;
            AppWindow.Resize(new SizeInt32((int)(1280 * scale), (int)(820 * scale)));
            var wa = DisplayArea.GetFromWindowId(AppWindow.Id, DisplayAreaFallback.Primary).WorkArea;
            AppWindow.Move(new PointInt32(wa.X + (wa.Width - AppWindow.Size.Width) / 2, wa.Y + (wa.Height - AppWindow.Size.Height) / 2));
        }
        if (s.WindowMaximized && AppWindow.Presenter is OverlappedPresenter op) op.Maximize();
    }

    private DispatcherQueueTimerHolder? _placementTimer;

    private void SavePlacementSoon()
    {
        _placementTimer ??= new DispatcherQueueTimerHolder(DispatcherQueue, TimeSpan.FromSeconds(1), SavePlacement);
        _placementTimer.Restart();
    }

    private void SavePlacement()
    {
        if (!AppWindow.IsVisible) return;
        var s = AppSettings.Current;
        if (AppWindow.Presenter is OverlappedPresenter { State: var state })
        {
            if (state == OverlappedPresenterState.Minimized) return;
            s.WindowMaximized = state == OverlappedPresenterState.Maximized;
            if (state == OverlappedPresenterState.Restored)
                s.WindowRect = [AppWindow.Position.X, AppWindow.Position.Y, AppWindow.Size.Width, AppWindow.Size.Height];
        }
        s.Save();
    }

    /** Однократный отложенный вызов (перезапуск откладывает). */
    private sealed class DispatcherQueueTimerHolder
    {
        private readonly Microsoft.UI.Dispatching.DispatcherQueueTimer _t;

        public DispatcherQueueTimerHolder(Microsoft.UI.Dispatching.DispatcherQueue q, TimeSpan delay, Action action)
        {
            _t = q.CreateTimer();
            _t.Interval = delay;
            _t.IsRepeating = false;
            _t.Tick += (_, _) => action();
        }

        public void Restart() { _t.Stop(); _t.Start(); }
    }
}
