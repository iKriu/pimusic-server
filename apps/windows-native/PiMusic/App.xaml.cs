using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using PiMusic.Data;
using PiMusic.Player;

namespace PiMusic;

public partial class App : Application
{
    public static MainWindow? Window { get; private set; }

    public App()
    {
        InitializeComponent();
        UnhandledException += (_, e) =>
        {
            // Ошибка в обработчике интерфейса не должна ронять плеер: записать, показать и продолжить.
            CrashLog(e.Exception);
            if (Window is null) return;
            e.Handled = true;
            Views.Ui.Error(e.Exception);
        };
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        try
        {
            Window = new MainWindow();
            var ui = DispatcherQueue.GetForCurrentThread();
            OfflineDownloads.Instance.Init();
            PlayerService.Instance.Init(ui);
            // эквалайзер из локальной копии настроек — сразу, до синхронизации с сервером
            SettingsStore.Instance.SetLogin(AppSettings.Current.Login);
            EqController.Instance.Init(ui);
            Services.TrayService.Attach(Window);
            if (Api.Instance.LoggedIn) Window.ShowMain(); else Window.ShowLogin();
            Window.Activate();
        }
        catch (Exception e)
        {
            CrashLog(e);
            throw;
        }
    }

    /** %LOCALAPPDATA%\PiMusic\crash.log — последние сбои (файл не растёт больше ~256 КБ). */
    public static void CrashLog(Exception e)
    {
        try
        {
            Directory.CreateDirectory(AppSettings.Dir);
            var path = Path.Combine(AppSettings.Dir, "crash.log");
            if (File.Exists(path) && new FileInfo(path).Length > 256 * 1024) File.Delete(path);
            File.AppendAllText(path, $"[{DateTimeOffset.Now:u}] {Api.Version}\n{e}\n\n");
        }
        catch (Exception) { /* журнал необязателен */ }
    }

    /** Второй запуск (ярлык, повторный клик) — показать уже открытое окно (Program.Main). */
    public static void BringToFront() => Services.TrayService.ShowWindow();
}
