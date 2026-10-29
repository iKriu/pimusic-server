using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Navigation;
using PiMusic.Data;

namespace PiMusic.Views;

/** Страница каталога. Параметр — ("album", id) или ("artist", id). */
public sealed partial class CatalogPage : Page
{
    private string? _token;

    public CatalogPage() => InitializeComponent();

    protected override void OnNavigatedTo(NavigationEventArgs e)
    {
        Templates.CatalogDownload = t => _ = Catalog.DownloadTrack(_token, t);
        if (e.Parameter is ValueTuple<string, string> (var kind, var id)) _ = Load(kind, id);
    }

    private async Task Load(string kind, string id)
    {
        Busy.IsActive = true;
        try
        {
            var keys = Catalog.Keys();
            if (kind == "album")
            {
                var a = await Api.Instance.CatalogAlbum(id);
                _token = a.Token;
                Catalog.Mark(a.Tracks, await keys);
                KindText.Text = a.Kind switch { "single" => "Сингл", "ep" => "EP", "compilation" => "Сборник", _ => "Альбом" };
                TitleText.Text = a.Title;
                InfoText.Text = Ui.Join(a.Artist, a.Year, Fmt.Tracks(a.Tracks.Count));
                Cover.Source = Ui.Img(a.CoverUri, 200);
                DownloadAllText.Text = "Скачать альбом";
                Tracks.ItemsSource = a.Tracks;
            }
            else
            {
                var a = await Api.Instance.CatalogArtist(id);
                _token = a.Token;
                Catalog.Mark(a.Tracks, await keys);
                KindText.Text = "Исполнитель";
                TitleText.Text = a.Name;
                InfoText.Text = a.Fans > 0 ? $"{a.Fans:N0} слушателей" : "";
                CoverBox.Visibility = Visibility.Collapsed;
                Photo.Visibility = Visibility.Visible;
                Photo.DisplayName = a.Name;
                Photo.ProfilePicture = Ui.Img(a.PictureUri, 168);
                DownloadAllText.Text = "Скачать популярные треки";
                TracksHeader.Visibility = Visibility.Visible;
                Tracks.ItemsSource = a.Tracks;
                Albums.ItemsSource = a.Albums;
                AlbumsHeader.Visibility = a.Albums.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
            }
            DownloadAllButton.Visibility = (Tracks.ItemsSource as List<CTrack>)?.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
        }
        catch (Exception ex) { Ui.Error(ex); }
        finally { Busy.IsActive = false; }
    }

    private async void DownloadAll_Click(object sender, RoutedEventArgs e)
    {
        DownloadAllButton.IsEnabled = false;
        await Catalog.Download(_token, "all");
        DownloadAllButton.IsEnabled = true;
    }

    private void Albums_ItemClick(object sender, ItemClickEventArgs e)
    {
        if (e.ClickedItem is CAlbum { Id: { } id }) App.Window?.Navigate(typeof(CatalogPage), ("album", id));
    }
}
