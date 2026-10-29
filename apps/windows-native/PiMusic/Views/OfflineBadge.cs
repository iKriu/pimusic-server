using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using PiMusic.Player;

namespace PiMusic.Views;

/**
 * Значок «скачано на компьютер» в строке трека: виден, пока трек TrackId лежит в OfflineDownloads. Содержимое
 * (FontIcon с цветом темы) задаёт шаблон; сам значок следит за Changed — x:Bind в шаблоне только однократный.
 */
public sealed partial class OfflineBadge : UserControl
{
    public static readonly DependencyProperty TrackIdProperty = DependencyProperty.Register(
        nameof(TrackId), typeof(long), typeof(OfflineBadge), new PropertyMetadata(0L, (d, _) => ((OfflineBadge)d).Update()));

    public long TrackId
    {
        get => (long)GetValue(TrackIdProperty);
        set => SetValue(TrackIdProperty, value);
    }

    public OfflineBadge()
    {
        Visibility = Visibility.Collapsed;
        Loaded += (_, _) =>
        {
            // Loaded приходит и повторно (перенос в дереве) — без двойной подписки
            OfflineDownloads.Instance.Changed -= Update;
            OfflineDownloads.Instance.Changed += Update;
            Update();
        };
        Unloaded += (_, _) => OfflineDownloads.Instance.Changed -= Update;
    }

    private void Update() => Visibility = Ui.Show(OfflineDownloads.Instance.IsDone(TrackId));
}
