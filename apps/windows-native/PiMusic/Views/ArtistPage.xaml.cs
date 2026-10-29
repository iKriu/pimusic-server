using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Navigation;
using PiMusic.Data;
using PiMusic.Player;

namespace PiMusic.Views;

/** Исполнитель медиатеки: фото, «Слушать»/«Перемешать», треки по альбомам. Параметр — artist_key. */
public sealed partial class ArtistPage : Page
{
    private LibArtistPage? _artist;

    public ArtistPage()
    {
        InitializeComponent();
        TrackActions.Attach(List, AllTracks, ids => _ = Load());
    }

    private string Key { get; set; } = "";

    private IReadOnlyList<Track> AllTracks() => _artist?.Albums.SelectMany(a => a.Tracks).ToList() ?? [];

    protected override void OnNavigatedTo(NavigationEventArgs e)
    {
        Key = e.Parameter as string ?? "";
        _ = Load();
    }

    private async Task Load()
    {
        Busy.IsActive = true;
        try
        {
            _artist = await Api.Instance.Artist(Key);
            NameText.Text = _artist.Name;
            Photo.DisplayName = _artist.Name;
            Photo.ProfilePicture = Ui.Img(_artist.ImageUri, 144);
            InfoText.Text = Ui.Join(Fmt.Tracks(_artist.Count), _artist.Albums.Count switch
            {
                1 => "1 альбом",
                var n and >= 2 and <= 4 => $"{n} альбома",
                var n => $"{n} альбомов",
            });
            Groups.Source = _artist.Albums;
        }
        catch (Exception ex) { Ui.Error(ex); }
        finally { Busy.IsActive = false; }
    }

    private void Play_Click(object sender, RoutedEventArgs e) => PlayerService.Instance.Play(AllTracks());
    private void Shuffle_Click(object sender, RoutedEventArgs e) => PlayerService.Instance.Play(AllTracks(), 0, shuffle: true);

    private void Album_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { Tag: string album }) App.Window?.Navigate(typeof(AlbumPage), (Key, album));
    }
}
