using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Navigation;
using PiMusic.Data;
using PiMusic.Player;

namespace PiMusic.Views;

/** Список плейлистов, создание и вступление по ссылке. При каждом возврате на страницу список обновляется. */
public sealed partial class PlaylistsPage : Page
{
    public PlaylistsPage() => InitializeComponent();

    protected override void OnNavigatedTo(NavigationEventArgs e) => _ = Load();

    private async Task Load()
    {
        Busy.IsActive = Tiles.ItemsSource is null;
        try
        {
            var r = await Api.Instance.Playlists();
            var items = r.Items.OrderByDescending(p => p.UpdatedAt).ToList();
            Tiles.ItemsSource = items;
            CountText.Text = Count(items.Count);
            Empty.Visibility = items.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        }
        catch (Exception ex) { Ui.Error(ex); }
        finally { Busy.IsActive = false; }
    }

    private static string Count(int n)
    {
        var m10 = n % 10; var m100 = n % 100;
        var w = m10 == 1 && m100 != 11 ? "плейлист" : m10 is >= 2 and <= 4 && (m100 < 12 || m100 > 14) ? "плейлиста" : "плейлистов";
        return $"{n} {w}";
    }

    private async void New_Click(object sender, RoutedEventArgs e)
    {
        if (await PlaylistDialogs.NewPlaylist(XamlRoot) is { } p) App.Window?.Navigate(typeof(PlaylistPage), p.Id);
    }

    private async void Join_Click(object sender, RoutedEventArgs e)
    {
        await PlaylistDialogs.Join(XamlRoot, null);
    }

    private void Tiles_ItemClick(object sender, ItemClickEventArgs e)
    {
        if (e.ClickedItem is Playlist p) App.Window?.Navigate(typeof(PlaylistPage), p.Id);
    }

    private void Tiles_ContextRequested(UIElement sender, Microsoft.UI.Xaml.Input.ContextRequestedEventArgs e)
    {
        if ((e.OriginalSource as FrameworkElement)?.DataContext is not Playlist p) return;
        var m = new MenuFlyout();
        m.Items.Add(Item("Слушать", "", () => _ = Play(p, false)));
        m.Items.Add(Item("Перемешать", "", () => _ = Play(p, true)));
        m.Items.Add(new MenuFlyoutSeparator());
        m.Items.Add(Item("Переименовать", "", () => _ = Rename(p)));
        if (p.IsOwner) m.Items.Add(Item("Удалить", "", () => _ = Delete(p)));
        else m.Items.Add(Item("Выйти из плейлиста", "", () => _ = Leave(p)));
        var target = e.OriginalSource as FrameworkElement;
        if (e.TryGetPosition(target, out var pos)) m.ShowAt(target, new FlyoutShowOptions { Position = pos });
        else m.ShowAt(target);
        e.Handled = true;
    }

    private static MenuFlyoutItem Item(string text, string glyph, Action click)
    {
        var i = new MenuFlyoutItem { Text = text, Icon = new FontIcon { Glyph = glyph } };
        i.Click += (_, _) => click();
        return i;
    }

    private static async Task Play(Playlist p, bool shuffle)
    {
        try
        {
            var full = await Api.Instance.Playlist(p.Id);
            var tracks = full.Tracks ?? [];
            if (tracks.Count == 0) { Ui.Info("В плейлисте пока пусто"); return; }
            PlayerService.Instance.Play(tracks, 0, shuffle);
        }
        catch (Exception ex) { Ui.Error(ex); }
    }

    private async Task Rename(Playlist p)
    {
        if (await PlaylistDialogs.Rename(XamlRoot, p)) await Load();
    }

    private async Task Delete(Playlist p)
    {
        if (await PlaylistDialogs.Delete(XamlRoot, p)) await Load();
    }

    private async Task Leave(Playlist p)
    {
        if (await PlaylistDialogs.Leave(XamlRoot, p)) await Load();
    }
}
