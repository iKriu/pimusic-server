using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Navigation;
using PiMusic.Data;

namespace PiMusic.Views;

/** Сетка альбомов медиатеки. */
public sealed partial class AlbumsPage : Page
{
    private bool _ready;

    public AlbumsPage()
    {
        InitializeComponent();
        SortBox.Items.Add(new ComboBoxItem { Content = "Недавно добавленные", Tag = "added" });
        SortBox.Items.Add(new ComboBoxItem { Content = "Название", Tag = "name" });
        SortBox.Items.Add(new ComboBoxItem { Content = "Больше треков", Tag = "count" });
        SortBox.Items.Add(new ComboBoxItem { Content = "Год", Tag = "year" });
        SortBox.SelectedIndex = 0;
        _ready = true;
    }

    protected override void OnNavigatedTo(NavigationEventArgs e)
    {
        if (Tiles.ItemsSource is null) _ = Load();
    }

    private async Task Load()
    {
        Busy.IsActive = true;
        try
        {
            var r = await Api.Instance.Albums(sort: (SortBox.SelectedItem as ComboBoxItem)?.Tag as string ?? "added");
            Tiles.ItemsSource = r.Items;
            CountText.Text = $"{r.Items.Count} альбомов";
        }
        catch (Exception ex) { Ui.Error(ex); }
        finally { Busy.IsActive = false; }
    }

    private void SortBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_ready) _ = Load();
    }

    private void Grid_ItemClick(object sender, ItemClickEventArgs e)
    {
        if (e.ClickedItem is LibAlbum a) App.Window?.Navigate(typeof(AlbumPage), (a.ArtistKey, a.Key));
    }
}
