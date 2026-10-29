using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media.Imaging;
using Microsoft.UI.Xaml.Navigation;
using PiMusic.Data;
using QRCoder;
using Windows.System;

namespace PiMusic.Views;

public sealed partial class LoginPage : Page
{
    private CancellationTokenSource? _poll;
    private string? _link;

    public LoginPage()
    {
        InitializeComponent();
        ServerBox.Text = AppSettings.Current.Server;
        // адрес ещё не задан — поле сразу на виду
        ServerExpander.IsExpanded = string.IsNullOrEmpty(AppSettings.Current.Server);
    }

    protected override void OnNavigatedFrom(NavigationEventArgs e) => _poll?.Cancel();

    private async void Start_Click(object sender, RoutedEventArgs e)
    {
        ErrorBar.IsOpen = false;
        if (!ApplyServer()) return;
        StartButton.IsEnabled = false;
        try
        {
            var start = await Api.Instance.AuthStart(DeviceName());
            _link = start.Link;
            CodeText.Text = start.Code ?? "";
            QrImage.Source = _link is null ? null : await Qr(_link);
            OpenBotButton.IsEnabled = _link is not null;
            StartPanel.Visibility = Visibility.Collapsed;
            CodePanel.Visibility = Visibility.Visible;
            WaitText.Text = "Ждём подтверждения…";
            await OpenTelegram(_link);
            _poll = new CancellationTokenSource(TimeSpan.FromSeconds(start.ExpiresIn));
            await Poll(start.Nonce, _poll.Token);
        }
        catch (OperationCanceledException) { }
        catch (Exception ex) { Fail(ex is ApiException a ? a.Message : ex.Message); }
        finally { StartButton.IsEnabled = true; }
    }

    private async Task Poll(string nonce, CancellationToken ct)
    {
        while (true)
        {
            try { await Task.Delay(2000, ct); }
            catch (OperationCanceledException)
            {
                if (CodePanel.Visibility == Visibility.Visible) Fail("Время на вход истекло — начните заново.");
                return;
            }
            AuthPoll r;
            try { r = await Api.Instance.AuthPoll(nonce, ct); }
            catch (ApiException e) when (e.Status is 0 or >= 500) { continue; } // связь пропала — пробуем дальше
            catch (OperationCanceledException) { continue; }
            switch (r.Status)
            {
                case "pending":
                    continue;
                case "too_many_devices":
                    WaitText.Text = "Уже 20 устройств: выйдите на одном из них в боте (/devices) — вход продолжится сам.";
                    continue;
                case "ok" when r.Token is not null:
                    AppSettings.Current.Token = r.Token;
                    AppSettings.Current.Login = r.Login;
                    AppSettings.Current.Save();
                    App.Window?.ShowMain();
                    return;
                case "denied":
                    Fail("Вход отклонён в боте.");
                    return;
                default:
                    Fail(r.Message ?? "Время на вход истекло — начните заново.");
                    return;
            }
        }
    }

    private void Fail(string message)
    {
        _poll?.Cancel();
        StartPanel.Visibility = Visibility.Visible;
        CodePanel.Visibility = Visibility.Collapsed;
        ErrorBar.Message = message;
        ErrorBar.IsOpen = true;
    }

    private void Cancel_Click(object sender, RoutedEventArgs e)
    {
        _poll?.Cancel();
        StartPanel.Visibility = Visibility.Visible;
        CodePanel.Visibility = Visibility.Collapsed;
    }

    private async void OpenBot_Click(object sender, RoutedEventArgs e)
    {
        await OpenTelegram(_link);
    }

    /** Проверить и запомнить адрес сервера из поля. */
    private bool ApplyServer()
    {
        var s = ServerBox.Text.Trim().TrimEnd('/');
        if (s.EndsWith("/app")) s = s[..^4];
        if (s.Length == 0)
        {
            ServerExpander.IsExpanded = true;
            Fail("Укажите адрес своего сервера PiMusic, например https://music.example.com.");
            return false;
        }
        if (!s.Contains("://")) s = "https://" + s;
        if (!Uri.TryCreate(s, UriKind.Absolute, out var u) || u.Scheme is not ("http" or "https"))
        {
            Fail("Неверный адрес сервера.");
            return false;
        }
        if (u.Scheme == "http" && !IsLocal(u))
        {
            Fail("Без https можно подключаться только в домашней сети — иначе токен уйдёт открытым текстом.");
            return false;
        }
        AppSettings.Current.Server = s;
        AppSettings.Current.Save();
        ServerBox.Text = s;
        return true;
    }

    /** https://t.me/<бот>?start=… → tg://resolve?domain=<бот>&start=… (приложение Telegram, если установлено). */
    /** null — ссылка не на Telegram (такую не открываем: адрес пришёл с сервера). */
    private static async Task<Uri?> TelegramUri(string link)
    {
        if (!Uri.TryCreate(link, UriKind.Absolute, out var u) || u.Scheme != "https" || u.Host is not ("t.me" or "telegram.me")) return null;
        var bot = u.AbsolutePath.Trim('/');
        if (!System.Text.RegularExpressions.Regex.IsMatch(bot, "^[A-Za-z0-9_]{3,64}$")) return null;
        var start = System.Web.HttpUtility.ParseQueryString(u.Query)["start"];
        var tg = new Uri($"tg://resolve?domain={bot}" + (start is null ? "" : "&start=" + Uri.EscapeDataString(start)));
        return await Launcher.QueryUriSupportAsync(tg, LaunchQuerySupportType.Uri) == LaunchQuerySupportStatus.Available ? tg : u;
    }

    private static async Task OpenTelegram(string? link)
    {
        if (link is not null && await TelegramUri(link) is { } uri) await Launcher.LaunchUriAsync(uri);
    }

    /** http допустим только в домашней сети: localhost, частные адреса, *.local / *.lan. */
    private static bool IsLocal(Uri u)
    {
        if (u.IsLoopback || u.Host.EndsWith(".local") || u.Host.EndsWith(".lan")) return true;
        if (!System.Net.IPAddress.TryParse(u.Host, out var ip)) return false;
        var b = ip.GetAddressBytes();
        return ip.AddressFamily == System.Net.Sockets.AddressFamily.InterNetwork
            && (b[0] == 10 || b[0] == 172 && b[1] is >= 16 and <= 31 || b[0] == 192 && b[1] == 168);
    }

    private static async Task<BitmapImage> Qr(string text)
    {
        var png = new PngByteQRCode(QRCodeGenerator.GenerateQrCode(text, QRCodeGenerator.ECCLevel.M)).GetGraphic(8);
        var bmp = new BitmapImage();
        using var ms = new Windows.Storage.Streams.InMemoryRandomAccessStream();
        await ms.WriteAsync(System.Runtime.InteropServices.WindowsRuntime.WindowsRuntimeBufferExtensions.AsBuffer(png));
        ms.Seek(0);
        await bmp.SetSourceAsync(ms);
        return bmp;
    }

    private static string DeviceName() => $"Windows · {Environment.MachineName}";
}
