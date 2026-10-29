using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Navigation;
using PiMusic.Data;

namespace PiMusic.Views;

/** Сетка исполнителей медиатеки (кругом — фото, как в «Медиаплеере»). */
public sealed partial class ArtistsPage : Page
{
    private bool _ready;

    public ArtistsPage()
    {
        InitializeComponent();
        Tiles.ItemTemplate = (DataTemplate)Application.Current.Resources["ArtistTile"];
        SortBox.Items.Add(new ComboBoxItem { Content = "По имени", Tag = "name" });
        SortBox.Items.Add(new ComboBoxItem { Content = "Больше треков", Tag = "count" });
        SortBox.Items.Add(new ComboBoxItem { Content = "Недавно добавленные", Tag = "added" });
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
            var r = await Api.Instance.Artists(sort: (SortBox.SelectedItem as ComboBoxItem)?.Tag as string ?? "name");
            Tiles.ItemsSource = r.Items;
            CountText.Text = $"{r.Items.Count} исполнителей";
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
        if (e.ClickedItem is LibArtist a) App.Window?.Navigate(typeof(ArtistPage), a.Key);
    }
}
