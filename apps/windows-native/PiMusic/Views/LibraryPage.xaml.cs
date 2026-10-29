using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Navigation;
using PiMusic.Data;
using PiMusic.Player;

namespace PiMusic.Views;

public sealed partial class LibraryPage : Page
{
    private TrackFeed? _feed;
    private long _seed = Random.Shared.NextInt64(1, int.MaxValue);
    private readonly DispatcherTimer _filterDelay = new() { Interval = TimeSpan.FromMilliseconds(350) };
    private bool _ready;

    public LibraryPage()
    {
        InitializeComponent();
        TrackActions.Attach(List, onDeleted: ids => _feed?.RemoveIds(ids));
        _filterDelay.Tick += (_, _) => { _filterDelay.Stop(); Reload(); };
    }

    protected override async void OnNavigatedTo(NavigationEventArgs e)
    {
        if (_ready) return;
        _ready = true;
        Reload();
        try
        {
            var genres = await Api.Instance.Genres();
            GenreBox.Items.Add(new ComboBoxItem { Content = "Все жанры", Tag = "" });
            foreach (var g in genres.Items) GenreBox.Items.Add(new ComboBoxItem { Content = $"{g.Name} ({g.Count})", Tag = g.Name });
        }
        catch (Exception) { /* без жанров список всё равно работает */ }
    }

    private string Sort => (SortBox.SelectedItem as ComboBoxItem)?.Tag as string ?? "added";
    private string? Genre => (GenreBox.SelectedItem as ComboBoxItem)?.Tag as string is { Length: > 0 } g ? g : null;
    private string? Query => FilterBox.Text.Trim() is { Length: > 0 } q ? q : null;

    private void Reload()
    {
        if (!_ready) return;
        var (q, genre, sort, seed) = (Query, Genre, Sort, _seed);
        var feed = new TrackFeed(offset => Api.Instance.Library(q, genre, sort, offset: offset, limit: 100, seed: sort == "random" ? seed : null));
        _feed = feed;
        feed.Loaded += () => { if (ReferenceEquals(feed, _feed)) UpdateState(); };
        feed.Failed += ex =>
        {
            if (!ReferenceEquals(feed, _feed)) return;
            Busy.IsActive = false;
            Ui.Error(ex);
        };
        Busy.IsActive = true;
        Empty.Visibility = Visibility.Collapsed;
        List.ItemsSource = _feed;
    }

    private void UpdateState()
    {
        Busy.IsActive = false;
        if (_feed is null) return;
        CountText.Text = _feed.Total == _feed.LibraryTotal || Query is null && Genre is null
            ? Fmt.Tracks(_feed.LibraryTotal)
            : $"Найдено {_feed.Total} из {_feed.LibraryTotal}";
        var empty = _feed.Count == 0;
        Empty.Visibility = empty ? Visibility.Visible : Visibility.Collapsed;
        EmptyText.Text = _feed.LibraryTotal == 0 ? "В медиатеке пока пусто" : "Ничего не найдено";
        EmptyAction.Visibility = _feed.LibraryTotal == 0 ? Visibility.Visible : Visibility.Collapsed;
    }

    private void FilterBox_TextChanged(AutoSuggestBox sender, AutoSuggestBoxTextChangedEventArgs args)
    {
        if (args.Reason != AutoSuggestionBoxTextChangeReason.UserInput) return;
        _filterDelay.Stop();
        _filterDelay.Start();
    }

    private void Filter_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (Sort == "random") _seed = Random.Shared.NextInt64(1, int.MaxValue);
        Reload();
    }

    private async void PlayAll_Click(object sender, RoutedEventArgs e) => await PlayView(shuffle: false);
    private async void Shuffle_Click(object sender, RoutedEventArgs e) => await PlayView(shuffle: true);

    /** «Слушать»/«Перемешать» — вся текущая выборка (до 3000 треков) в порядке списка. */
    private async Task PlayView(bool shuffle)
    {
        try
        {
            var all = await Api.Instance.LibraryPlay(Query, Genre, Sort, seed: Sort == "random" ? _seed : null);
            if (all.Items.Count == 0) return;
            PlayerService.Instance.Play(all.Items, 0, shuffle);
        }
        catch (Exception ex) { Ui.Error(ex); }
    }

    private void EmptyAction_Click(object sender, RoutedEventArgs e) => App.Window?.Navigate(typeof(SearchPage));
}
