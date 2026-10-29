using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Navigation;
using PiMusic.Data;

namespace PiMusic.Views;

/** Поиск по медиатеке и каталогу. Параметр — строка запроса (из поиска в заголовке окна) или null. */
public sealed partial class SearchPage : Page
{
    private SearchResult? _result;
    private CancellationTokenSource? _cts;

    public SearchPage()
    {
        InitializeComponent();
        TrackActions.Attach(LibraryList);
    }

    protected override void OnNavigatedTo(NavigationEventArgs e)
    {
        Templates.CatalogDownload = t => _ = Catalog.DownloadTrack(_result?.Token, t);
        if (e.Parameter is string q && q.Length > 0 && e.NavigationMode != NavigationMode.Back)
        {
            QueryBox.Text = q;
            _ = Run(q);
        }
        else if (_result is null) QueryBox.Focus(FocusState.Programmatic);
    }

    private void QueryBox_QuerySubmitted(AutoSuggestBox sender, AutoSuggestBoxQuerySubmittedEventArgs args)
    {
        var q = args.QueryText.Trim();
        if (q.Length > 0) _ = Run(q);
    }

    private static bool IsLink(string q) =>
        Uri.TryCreate(q, UriKind.Absolute, out var u) && u.Scheme is "http" or "https";

    private async Task Run(string q)
    {
        _cts?.Cancel();
        _cts = new CancellationTokenSource();
        var ct = _cts.Token;
        LinkBar.IsOpen = IsLink(q);
        LibrarySection.Visibility = CatalogSection.Visibility = EmptyText.Visibility = Visibility.Collapsed;
        if (LinkBar.IsOpen)
        {
            Busy.IsActive = false;
            return;
        }
        Busy.IsActive = true;
        try
        {
            var lib = Api.Instance.Library(q, sort: "title", limit: 5, ct: ct);
            var cat = Api.Instance.Search(q, ct);
            var keys = Catalog.Keys();

            var mine = await lib;
            if (ct.IsCancellationRequested) return;
            LibraryList.ItemsSource = mine.Items;
            LibrarySection.Visibility = mine.Items.Count > 0 ? Visibility.Visible : Visibility.Collapsed;

            _result = await cat;
            if (ct.IsCancellationRequested) return;
            Catalog.Mark(_result.Tracks, await keys);
            CatalogTracks.ItemsSource = _result.Tracks;
            CatalogAlbums.ItemsSource = _result.Albums;
            CatalogArtists.ItemsSource = _result.Artists;
            var any = _result.Tracks.Count + _result.Albums.Count + _result.Artists.Count > 0;
            CatalogSection.Visibility = any ? Visibility.Visible : Visibility.Collapsed;
            if (!any && mine.Items.Count == 0)
            {
                EmptyText.Text = $"По запросу «{q}» ничего не найдено";
                EmptyText.Visibility = Visibility.Visible;
            }
            ShowTab();
        }
        catch (OperationCanceledException) { }
        catch (Exception ex) { Ui.Error(ex); }
        finally { if (!ct.IsCancellationRequested) Busy.IsActive = false; }
    }

    private void Tabs_SelectionChanged(SelectorBar sender, SelectorBarSelectionChangedEventArgs args) => ShowTab();

    private void ShowTab()
    {
        var sel = Tabs.SelectedItem;
        CatalogTracks.Visibility = sel == TracksTab ? Visibility.Visible : Visibility.Collapsed;
        CatalogAlbums.Visibility = sel == AlbumsTab ? Visibility.Visible : Visibility.Collapsed;
        CatalogArtists.Visibility = sel == ArtistsTab ? Visibility.Visible : Visibility.Collapsed;
        DownloadAllButton.Visibility = sel == TracksTab && _result is { Tracks.Count: > 1 } ? Visibility.Visible : Visibility.Collapsed;
    }

    private async void DownloadAll_Click(object sender, RoutedEventArgs e)
    {
        if (_result is null) return;
        DownloadAllButton.IsEnabled = false;
        await Catalog.Download(_result.Token, "all");
        DownloadAllButton.IsEnabled = true;
    }

    private async void DownloadLink_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var r = await Api.Instance.DownloadLink(QueryBox.Text.Trim());
            Ui.Success(Catalog.Queued(r));
            LinkBar.IsOpen = false;
        }
        catch (Exception ex) { Ui.Error(ex); }
    }

    private void CatalogAlbums_ItemClick(object sender, ItemClickEventArgs e)
    {
        if (e.ClickedItem is CAlbum { Id: { } id }) App.Window?.Navigate(typeof(CatalogPage), ("album", id));
    }

    private void CatalogArtists_ItemClick(object sender, ItemClickEventArgs e)
    {
        if (e.ClickedItem is CArtist { Id: { } id }) App.Window?.Navigate(typeof(CatalogPage), ("artist", id));
    }
}

/** Общее для страниц каталога: отметки «уже есть», скачивание. */
public static class Catalog
{
    public static async Task<LibKeys?> Keys()
    {
        try { return await Api.Instance.LibraryKeys(); }
        catch (Exception) { return null; }
    }

    public static void Mark(IEnumerable<CTrack> tracks, LibKeys? keys)
    {
        if (keys is null) return;
        var k = keys.Keys.ToHashSet();
        var isrc = keys.Isrc.ToHashSet(StringComparer.OrdinalIgnoreCase);
        foreach (var t in tracks)
            t.InLibrary = t.Key is not null && k.Contains(t.Key) || t.Isrc is not null && isrc.Contains(t.Isrc);
    }

    public static string Queued(DownloadResp r) =>
        r.Queued > 0 ? $"В очереди на скачивание: {Fmt.Tracks(r.Queued)}" + (r.Title.Length > 0 ? $" — {r.Title}" : "")
            : r.Note ?? "Уже в очереди или в медиатеке";

    public static async Task Download(string? token, string kind, int? i = null)
    {
        if (token is null) return;
        try
        {
            var r = await Api.Instance.Download(token, kind, i);
            Ui.Success(Queued(r));
            ActivityWatcher.Poke();
        }
        catch (ApiException e) when (e.Code == "expired") { Ui.Info("Результаты поиска устарели — повторите поиск."); }
        catch (Exception e) { Ui.Error(e); }
    }

    public static Task DownloadTrack(string? token, CTrack t) => Download(token, "track", t.I);
}
