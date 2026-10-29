using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Navigation;
using PiMusic.Data;
using PiMusic.Player;

namespace PiMusic.Views;

/** Альбом медиатеки. Параметр — (artist_key, album_key). */
public sealed partial class AlbumPage : Page
{
    private LibAlbumPage? _album;
    private (string Artist, string Album) _key;

    public AlbumPage()
    {
        InitializeComponent();
        TrackActions.Attach(List, onDeleted: ids => _ = Load());
    }

    protected override void OnNavigatedTo(NavigationEventArgs e)
    {
        if (e.Parameter is ValueTuple<string, string> k) _key = k;
        _ = Load();
    }

    private async Task Load()
    {
        Busy.IsActive = true;
        try
        {
            _album = await Api.Instance.Album(_key.Artist, _key.Album);
            TitleText.Text = _album.Title;
            ArtistLink.Content = _album.Artist;
            Cover.Source = Ui.Img(_album.CoverUri, 200);
            var year = _album.Tracks.Select(t => t.Year).FirstOrDefault(y => y is > 0);
            var seconds = _album.Tracks.Sum(t => t.Seconds);
            InfoText.Text = Ui.Join(Ui.Year(year), Fmt.Tracks(_album.Tracks.Count), Fmt.Time(TimeSpan.FromSeconds(seconds)));
            List.ItemsSource = _album.Tracks;
        }
        catch (Exception ex) { Ui.Error(ex); }
        finally { Busy.IsActive = false; }
    }

    private void Play_Click(object sender, RoutedEventArgs e)
    {
        if (_album is not null) PlayerService.Instance.Play(_album.Tracks);
    }

    private void Shuffle_Click(object sender, RoutedEventArgs e)
    {
        if (_album is not null) PlayerService.Instance.Play(_album.Tracks, 0, shuffle: true);
    }

    private void Offline_Click(object sender, RoutedEventArgs e)
    {
        if (_album is not null) TrackActions.SaveOffline(_album.Tracks, _album.Title);
    }

    private void Artist_Click(object sender, RoutedEventArgs e)
    {
        if (_album is not null) App.Window?.Navigate(typeof(ArtistPage), _album.ArtistKey);
    }
}
