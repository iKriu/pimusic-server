using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Navigation;
using PiMusic.Data;
using PiMusic.Player;
using PiMusic.Services;
using PiMusic.Update;

namespace PiMusic.Views;

public sealed partial class SettingsPage : Page
{
    private readonly AppSettings _s = AppSettings.Current;
    private UpdateInfo? _update;
    private bool _ready;

    public string Server => _s.Server;
    public string VersionText => $"Версия {Updater.CurrentVersion}";

    public SettingsPage()
    {
        InitializeComponent();
        NormalizeSwitch.IsOn = _s.Normalize;
        TraySwitch.IsOn = _s.CloseToTray;
        NotifySwitch.IsOn = _s.TrackNotifications;
        ThemeBox.SelectedItem = ThemeBox.Items.OfType<ComboBoxItem>().FirstOrDefault(i => (string)i.Tag == _s.Theme) ?? ThemeBox.Items[0];
        _ready = true;
    }

    protected override void OnNavigatedFrom(NavigationEventArgs e) => OfflineDownloads.Instance.Changed -= ShowOffline;

    protected override async void OnNavigatedTo(NavigationEventArgs e)
    {
        OfflineDownloads.Instance.Changed += ShowOffline;
        ShowOffline();
        AccountCard.Header = _s.Login is { Length: > 0 } l ? l : "Вход выполнен";
        AccountCard.Description = "Вход через Telegram-бота";
        try
        {
            var me = await Api.Instance.Me();
            _s.Login = me.Login;
            AccountCard.Header = me.Login;
            AccountCard.Description = Ui.Join(Fmt.Tracks(me.Tracks) + " в медиатеке", me.IsAdmin ? "администратор" : null);
        }
        catch (Exception) { /* без сети — показываем сохранённое */ }
    }

    // ---------------------------------------------------------------- аккаунт

    private async void Logout_Click(object sender, RoutedEventArgs e)
    {
        if (!await Ui.Confirm(XamlRoot, "Выйти из аккаунта?", "Музыка остановится, очередь будет очищена.", "Выйти")) return;
        PlayerService.Instance.ClearQueue();
        try { await Api.Instance.Logout(); }
        catch (Exception) { /* токен всё равно стёрт локально */ }
        App.Window?.ShowLogin();
    }

    private async void Devices_Expanded(object sender, EventArgs e)
    {
        DevicesLoading.Visibility = Visibility.Visible;
        try { DevicesExpander.ItemsSource = (await Api.Instance.Devices()).Items; }
        catch (Exception ex) { Ui.Error(ex); }
        finally { DevicesLoading.Visibility = Visibility.Collapsed; }
    }

    private async void Revoke_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: Device d }) return;
        if (!await Ui.Confirm(XamlRoot, "Выйти на устройстве?", $"«{d.Name}» потеряет доступ к PiMusic.", "Выйти")) return;
        try
        {
            await Api.Instance.RevokeDevice(d.Id);
            Devices_Expanded(sender, EventArgs.Empty);
        }
        catch (Exception ex) { Ui.Error(ex); }
    }

    private async void ChangeServer_Click(object sender, RoutedEventArgs e)
    {
        // скачанное привязано к id треков этого сервера — на другом те же id значат другие треки
        var off = OfflineDownloads.Instance;
        var text = "Для другого сервера нужно войти заново." + (off.Items.Count > 0 ? " Скачанное на компьютере будет удалено." : "");
        if (!await Ui.Confirm(XamlRoot, "Сменить сервер?", text, "Продолжить")) return;
        PlayerService.Instance.ClearQueue();
        off.RemoveAll();
        try { await Api.Instance.Logout(); }
        catch (Exception) { }
        App.Window?.ShowLogin();
    }

    // ---------------------------------------------------------------- без сети

    private void ShowOffline()
    {
        var off = OfflineDownloads.Instance;
        var busy = off.ActiveCount;
        OfflineCard.Description = off.Items.Count == 0
            ? "Пока ничего — «Скачать на компьютер» в меню трека, альбома или плейлиста"
            : Fmt.Join(Fmt.Tracks(off.DoneCount), Fmt.Size(off.DiskBytes) + " на диске", busy > 0 ? $"ещё {busy} в очереди" : null);
        ClearOfflineButton.IsEnabled = off.Items.Count > 0;
    }

    private void Offline_Click(object sender, RoutedEventArgs e) => App.Window?.Navigate(typeof(OfflinePage));

    private void OfflineFolder_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            Directory.CreateDirectory(OfflineDownloads.Dir);
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo("explorer.exe", $"\"{OfflineDownloads.Dir}\"") { UseShellExecute = true });
        }
        catch (Exception ex) { Ui.Error(ex); }
    }

    private async void ClearOffline_Click(object sender, RoutedEventArgs e)
    {
        if (await Ui.Confirm(XamlRoot, "Удалить всё скачанное?", "Треки останутся в медиатеке на сервере.", "Удалить"))
            OfflineDownloads.Instance.RemoveAll();
    }

    // ---------------------------------------------------------------- переключатели

    private void Equalizer_Click(object sender, RoutedEventArgs e) => App.Window?.Navigate(typeof(EqualizerPage));

    private void NormalizeSwitch_Toggled(object sender, RoutedEventArgs e)
    {
        if (!_ready) return;
        _s.Normalize = NormalizeSwitch.IsOn;
        _s.Save();
        PlayerService.Instance.RefreshVolume();
    }

    private void TraySwitch_Toggled(object sender, RoutedEventArgs e)
    {
        if (!_ready) return;
        _s.CloseToTray = TraySwitch.IsOn;
        _s.Save();
    }

    private void NotifySwitch_Toggled(object sender, RoutedEventArgs e)
    {
        if (!_ready) return;
        _s.TrackNotifications = NotifySwitch.IsOn;
        _s.Save();
    }

    private void ThemeBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!_ready || ThemeBox.SelectedItem is not ComboBoxItem { Tag: string theme }) return;
        _s.Theme = theme;
        _s.Save();
        App.Window?.ApplyTheme(theme);
    }

    // ---------------------------------------------------------------- обновления

    private async void Check_Click(object sender, RoutedEventArgs e)
    {
        CheckButton.IsEnabled = false;
        try
        {
            _update = await Updater.Instance.CheckAsync(manual: true);
            if (_update is null)
            {
                Ui.Success("У вас последняя версия");
                return;
            }
            UpdateCard.Header = $"Доступна версия {_update.Version}";
            UpdateCard.Description = _update.Notes;
            UpdateCard.Visibility = Visibility.Visible;
        }
        catch (Exception ex) { Ui.Error(ex); }
        finally { CheckButton.IsEnabled = true; }
    }

    private async void Install_Click(object sender, RoutedEventArgs e)
    {
        if (_update is null) return;
        InstallButton.IsEnabled = SkipButton.IsEnabled = false;
        UpdateProgress.Visibility = Visibility.Visible;
        try
        {
            await Updater.Instance.InstallAsync(_update, new Progress<double>(p => UpdateProgress.Value = p), CancellationToken.None);
        }
        catch (Exception ex)
        {
            Ui.Error(ex);
            InstallButton.IsEnabled = SkipButton.IsEnabled = true;
            UpdateProgress.Visibility = Visibility.Collapsed;
        }
    }

    private void Skip_Click(object sender, RoutedEventArgs e)
    {
        if (_update is not null) Updater.Instance.Skip(_update);
        UpdateCard.Visibility = Visibility.Collapsed;
    }

    private void Exit_Click(object sender, RoutedEventArgs e) => TrayService.ExitApp();
}
