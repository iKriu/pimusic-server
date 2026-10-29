using Microsoft.UI.Xaml;
using PiMusic.Data;

namespace PiMusic.Views;

/**
 * Ход загрузок (GET /api/activity): при входе по токену бот не пишет о загрузках в Telegram, поэтому клиент
 * опрашивает сам — раз в 3 с, пока что-то качается или открыта страница «Загрузки», иначе раз в 30 с.
 * Значок на пункте «Загрузки» — число активных; по окончании — сообщение «Загрузки завершены».
 */
public static class ActivityWatcher
{
    private static readonly DispatcherTimer Timer = new();
    private static bool _busy;
    private static int _lastActive;

    public static Activity? Last { get; private set; }
    public static event Action<Activity>? Changed;
    /** Открыта страница «Загрузки» — опрашивать чаще. */
    public static bool Watching { get; set; }

    public static void Start()
    {
        Timer.Tick -= Tick;
        Timer.Tick += Tick;
        Poke();
    }

    public static void Stop() => Timer.Stop();

    /** Опросить сейчас (после «Скачать» или при открытии страницы). */
    public static void Poke()
    {
        Timer.Interval = TimeSpan.FromSeconds(1);
        Timer.Start();
    }

    private static async void Tick(object? sender, object e)
    {
        if (_busy || !Api.Instance.LoggedIn) return;
        _busy = true;
        try
        {
            var a = await Api.Instance.Activity();
            Last = a;
            var active = a.ActiveItems.Count + a.Queue;
            App.Window?.SetDownloadsBadge(active);
            if (_lastActive > 0 && active == 0) Ui.Success("Загрузки завершены — треки в медиатеке");
            _lastActive = active;
            Changed?.Invoke(a);
            Timer.Interval = TimeSpan.FromSeconds(active > 0 || Watching ? 3 : 30);
        }
        catch (Exception) { Timer.Interval = TimeSpan.FromSeconds(30); }
        finally { _busy = false; }
    }
}
