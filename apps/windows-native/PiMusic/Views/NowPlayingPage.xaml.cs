using System.ComponentModel;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Navigation;
using PiMusic.Data;
using PiMusic.Player;

namespace PiMusic.Views;

public sealed partial class NowPlayingPage : Page
{
    public PlayerService P { get; } = PlayerService.Instance;

    private long _lyricsFor = -1;
    private List<LyricLine>? _synced;
    private int _line = -1;

    public NowPlayingPage() => InitializeComponent();

    public static bool HasError(string? e) => !string.IsNullOrEmpty(e);

    protected override void OnNavigatedTo(NavigationEventArgs e)
    {
        P.PropertyChanged += Player_PropertyChanged;
        SyncQueueSelection();
        if (Tabs.SelectedItem == LyricsTab) _ = LoadLyrics();
    }

    protected override void OnNavigatedFrom(NavigationEventArgs e) => P.PropertyChanged -= Player_PropertyChanged;

    private void Player_PropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        switch (e.PropertyName)
        {
            case nameof(PlayerService.Index):
                SyncQueueSelection();
                break;
            case nameof(PlayerService.Current):
                if (Tabs.SelectedItem == LyricsTab) _ = LoadLyrics();
                break;
            case nameof(PlayerService.Position):
                HighlightLine();
                break;
        }
    }

    private void SyncQueueSelection()
    {
        if (P.Index >= 0 && P.Index < P.Queue.Count)
        {
            QueueList.SelectedIndex = P.Index;
            QueueList.ScrollIntoView(P.Queue[P.Index]);
        }
    }

    private void Tabs_SelectionChanged(SelectorBar sender, SelectorBarSelectionChangedEventArgs args)
    {
        var lyrics = sender.SelectedItem == LyricsTab;
        QueueList.Visibility = lyrics ? Visibility.Collapsed : Visibility.Visible;
        ClearButton.Visibility = lyrics ? Visibility.Collapsed : Visibility.Visible;
        LyricsPanel.Visibility = lyrics ? Visibility.Visible : Visibility.Collapsed;
        if (lyrics) _ = LoadLyrics();
    }

    // ---------------------------------------------------------------- очередь

    private void QueueList_DoubleTapped(object sender, DoubleTappedRoutedEventArgs e)
    {
        if (IndexOf(e.OriginalSource) is var i and >= 0) P.JumpTo(i);
    }

    private void QueueList_KeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (QueueList.SelectedIndex < 0) return;
        if (e.Key == Windows.System.VirtualKey.Enter) P.JumpTo(QueueList.SelectedIndex);
        else if (e.Key == Windows.System.VirtualKey.Delete) P.RemoveAt(QueueList.SelectedIndex);
        else return;
        e.Handled = true;
    }

    private void QueueList_ContextRequested(UIElement sender, ContextRequestedEventArgs e)
    {
        var i = IndexOf(e.OriginalSource);
        if (i < 0 || i >= P.Queue.Count) return;
        var t = P.Queue[i];
        var m = new MenuFlyout();
        Add(m, "Играть", "", () => P.JumpTo(i));
        Add(m, "Убрать из очереди", "", () => P.RemoveAt(i));
        if (i > 0) Add(m, "Выше", "", () => P.Move(i, i - 1));
        if (i < P.Queue.Count - 1) Add(m, "Ниже", "", () => P.Move(i, i + 1));
        if (!string.IsNullOrEmpty(t.ArtistKey))
        {
            m.Items.Add(new MenuFlyoutSeparator());
            Add(m, "К исполнителю", "", () => App.Window?.Navigate(typeof(ArtistPage), t.ArtistKey));
        }
        if (e.TryGetPosition(e.OriginalSource as UIElement, out var p))
            m.ShowAt(e.OriginalSource as FrameworkElement, new FlyoutShowOptions { Position = p });
        else m.ShowAt(e.OriginalSource as FrameworkElement);
        e.Handled = true;
    }

    /** Номер строки очереди под элементом (по контейнеру: один трек может стоять в очереди дважды). */
    private int IndexOf(object? source)
    {
        var d = source as DependencyObject;
        while (d is not null and not ListViewItem) d = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetParent(d);
        return d is ListViewItem item ? QueueList.IndexFromContainer(item) : -1;
    }

    private static void Add(MenuFlyout m, string text, string glyph, Action a)
    {
        var item = new MenuFlyoutItem { Text = text, Icon = new FontIcon { Glyph = glyph } };
        item.Click += (_, _) => a();
        m.Items.Add(item);
    }

    private async void Clear_Click(object sender, RoutedEventArgs e)
    {
        if (P.Queue.Count == 0) return;
        if (await Ui.Confirm(XamlRoot, "Очистить очередь?", "Воспроизведение остановится.", "Очистить")) P.ClearQueue();
    }

    // ---------------------------------------------------------------- текст

    private async Task LoadLyrics()
    {
        var t = P.Current;
        // у прямого эфира текста нет
        if (t is null || t.Live)
        {
            _lyricsFor = -1;
            ShowLyrics(null, null);
            return;
        }
        if (t.Id == _lyricsFor) return;
        _lyricsFor = t.Id;
        LyricsLoading.IsActive = true;
        LyricsList.Visibility = PlainScroll.Visibility = LyricsEmpty.Visibility = Visibility.Collapsed;
        try
        {
            var l = await Api.Instance.Lyrics(t.Id);
            if (P.Current?.Id != t.Id) { _lyricsFor = -1; return; }
            ShowLyrics(l.Synced is { Count: > 0 } ? l.Synced : null, l.Plain);
        }
        catch (ApiException e) when (e.Status == 404) { ShowLyrics(null, null); }
        catch (Exception e)
        {
            _lyricsFor = -1;
            ShowLyrics(null, null);
            LyricsEmpty.Text = e.Message;
        }
        finally { if (_lyricsFor == t.Id || _lyricsFor == -1) LyricsLoading.IsActive = false; }
    }

    private void ShowLyrics(List<LyricLine>? synced, string? plain)
    {
        _synced = synced;
        _line = -1;
        LyricsList.ItemsSource = synced;
        PlainText.Text = plain ?? "";
        LyricsList.Visibility = synced is null ? Visibility.Collapsed : Visibility.Visible;
        PlainScroll.Visibility = synced is null && !string.IsNullOrEmpty(plain) ? Visibility.Visible : Visibility.Collapsed;
        LyricsEmpty.Text = "Текста для этого трека нет";
        LyricsEmpty.Visibility = synced is null && string.IsNullOrEmpty(plain) ? Visibility.Visible : Visibility.Collapsed;
        HighlightLine();
    }

    /** Подсветить строку по времени и держать её в середине списка. */
    private void HighlightLine()
    {
        if (_synced is null || LyricsPanel.Visibility != Visibility.Visible) return;
        var i = _synced.FindLastIndex(l => l.T <= P.Position + 0.2);
        if (i == _line) return;
        _line = i;
        LyricsList.SelectedIndex = i;
        if (i >= 0) LyricsList.ScrollIntoView(_synced[i], ScrollIntoViewAlignment.Default);
    }

    private void LyricsList_ItemClick(object sender, ItemClickEventArgs e)
    {
        if (e.ClickedItem is LyricLine l) P.Seek(l.T);
    }

    // ---------------------------------------------------------------- переходы

    private void Artist_Click(object sender, RoutedEventArgs e)
    {
        if (P.Current?.ArtistKey is { Length: > 0 } a) App.Window?.Navigate(typeof(ArtistPage), a);
    }

    private void Album_Click(object sender, RoutedEventArgs e)
    {
        if (P.Current is { ArtistKey: { Length: > 0 } a, AlbumKey: { Length: > 0 } b }) App.Window?.Navigate(typeof(AlbumPage), (a, b));
    }
}
