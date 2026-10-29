using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Data;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Navigation;
using PiMusic.Data;
using PiMusic.Player;

namespace PiMusic.Views;

/** Группа «Скачанного»: альбом/плейлист, из которого скачивали, или «Отдельные треки». */
public sealed class OfflineGroup(string key, IEnumerable<OfflineItem> items) : List<OfflineItem>(items)
{
    public string Key { get; } = key;
}

/**
 * «Скачанное» (как Offline.kt в Android): треки на компьютере — размер, прогресс, повтор, удаление; «Слушать всё» —
 * всё скачанное очередью. Двойной щелчок — играть скачанное с этого трека, правая кнопка — обычное меню трека.
 */
public sealed partial class OfflinePage : Page
{
    private readonly OfflineDownloads _off = OfflineDownloads.Instance;
    private string _shape = "";

    public OfflinePage() => InitializeComponent();

    protected override void OnNavigatedTo(NavigationEventArgs e)
    {
        _off.Changed += Refresh;
        _shape = "";
        Refresh();
    }

    protected override void OnNavigatedFrom(NavigationEventArgs e) => _off.Changed -= Refresh;

    private List<Track> DoneTracks() => [.. _off.Items.Where(o => o.Done).Select(o => o.Track)];

    /** Сводка — всегда; список пересобирается, только если изменился состав (прогресс обновляют сами строки). */
    private void Refresh()
    {
        var all = _off.Items.ToList();
        var done = all.Count(o => o.Done);
        var busy = all.Count(o => o.Active);
        CountText.Text = all.Count == 0 ? "" : Fmt.Join(Fmt.Tracks(done), Fmt.Size(_off.DiskBytes), busy > 0 ? $"ещё {busy} в очереди" : null);
        Empty.Visibility = Ui.Show(all.Count == 0);
        Actions.Visibility = Ui.Hide(all.Count == 0);
        PlayButton.IsEnabled = ShuffleButton.IsEnabled = done > 0;

        var shape = string.Join(",", all.Select(o => o.Track.Id));
        if (shape == _shape) return;
        _shape = shape;
        var groups = all.GroupBy(o => o.Group ?? "")
            .OrderBy(g => g.Key.Length == 0 ? 1 : 0).ThenBy(g => g.Key, StringComparer.CurrentCultureIgnoreCase)
            .Select(g => new OfflineGroup(g.Key.Length == 0 ? "Отдельные треки" : g.Key, g))
            .ToList();
        List.ItemsSource = new CollectionViewSource { IsSourceGrouped = true, Source = groups }.View;
    }

    private void Play_Click(object sender, RoutedEventArgs e)
    {
        if (DoneTracks() is { Count: > 0 } t) PlayerService.Instance.Play(t);
    }

    private void Shuffle_Click(object sender, RoutedEventArgs e)
    {
        if (DoneTracks() is { Count: > 0 } t) PlayerService.Instance.Play(t, 0, shuffle: true);
    }

    private async void Clear_Click(object sender, RoutedEventArgs e)
    {
        if (await Ui.Confirm(XamlRoot, "Удалить всё скачанное?", "Треки останутся в медиатеке на сервере.", "Удалить"))
            _off.RemoveAll();
    }

    private void Retry_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as FrameworkElement)?.Tag is OfflineItem o) _off.Retry(o);
    }

    private void Remove_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as FrameworkElement)?.Tag is OfflineItem o) _off.Remove(o.Track.Id);
    }

    private void PlayFrom(OfflineItem o)
    {
        if (!o.Done) return;
        TrackActions.PlayFrom(DoneTracks(), o.Track);
    }

    private void List_DoubleTapped(object sender, DoubleTappedRoutedEventArgs e)
    {
        if ((e.OriginalSource as FrameworkElement)?.DataContext is OfflineItem o) PlayFrom(o);
    }

    private void List_KeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key == Windows.System.VirtualKey.Enter && List.SelectedItem is OfflineItem o)
        {
            PlayFrom(o);
            e.Handled = true;
        }
        else if (e.Key == Windows.System.VirtualKey.Delete && List.SelectedItems.OfType<OfflineItem>().ToList() is { Count: > 0 } sel)
        {
            foreach (var x in sel) _off.Remove(x.Track.Id);
            e.Handled = true;
        }
    }

    private void List_ContextRequested(UIElement sender, ContextRequestedEventArgs e)
    {
        var o = (e.OriginalSource as FrameworkElement)?.DataContext as OfflineItem ?? List.SelectedItem as OfflineItem;
        if (o is null) return;
        var selected = List.SelectedItems.OfType<OfflineItem>().Select(x => x.Track).ToList();
        if (!selected.Any(x => x.Id == o.Track.Id)) selected = [o.Track];
        var menu = TrackActions.Menu(XamlRoot, DoneTracks(), o.Track, selected, null);
        if (e.TryGetPosition(e.OriginalSource as UIElement, out var p))
            menu.ShowAt(e.OriginalSource as FrameworkElement, new FlyoutShowOptions { Position = p });
        else
            menu.ShowAt(e.OriginalSource as FrameworkElement);
        e.Handled = true;
    }
}
