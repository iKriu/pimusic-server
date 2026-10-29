using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.Windows.AppLifecycle;

namespace PiMusic;

/**
 * Точка входа: один экземпляр на пользователя. Повторный запуск передаёт активацию уже работающему PiMusic
 * (тот показывает окно, даже если оно спрятано в трей) и сразу завершается.
 */
public static partial class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
#if DEBUG
        // Отладка: все исключения (в том числе пойманные) — в журнал сбоев.
        AppDomain.CurrentDomain.FirstChanceException += (_, e) => App.CrashLog(e.Exception);
#endif
        WinRT.ComWrappersSupport.InitializeComWrappers();
        // Тот же AppUserModelID, что у ярлыка установщика: SMTC и уведомления привязываются к ярлыку PiMusic.
        SetCurrentProcessExplicitAppUserModelID("PiMusic.Desktop");
        if (Redirected()) return 0;

        Application.Start(p =>
        {
            var context = new DispatcherQueueSynchronizationContext(DispatcherQueue.GetForCurrentThread());
            SynchronizationContext.SetSynchronizationContext(context);
            try { _ = new App(); }
            catch (Exception e)
            {
                App.CrashLog(e);
                throw;
            }
        });
        return 0;
    }

    [System.Runtime.InteropServices.DllImport("shell32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    private static extern int SetCurrentProcessExplicitAppUserModelID(string appId);

    private static bool Redirected()
    {
        var current = AppInstance.GetCurrent();
        var main = AppInstance.FindOrRegisterForKey("PiMusic.main");
        if (main.IsCurrent)
        {
            main.Activated += (_, _) =>
            {
                var q = App.Window?.DispatcherQueue;
                q?.TryEnqueue(App.BringToFront);
            };
            return false;
        }
        // Ждать перенаправления в отдельном потоке: в STA-потоке без цикла сообщений await зависнет.
        var args = current.GetActivatedEventArgs();
        Task.Run(() => main.RedirectActivationToAsync(args).AsTask()).Wait();
        return true;
    }
}
