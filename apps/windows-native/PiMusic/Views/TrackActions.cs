using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Input;
using PiMusic.Data;
using PiMusic.Player;

namespace PiMusic.Views;

/**
 * Действия со списком треков: двойной щелчок/Enter — играть список с этого трека; контекстное меню (правая кнопка,
 * Shift+F10, клавиша меню) — «Играть», «Следующим», «В очередь», «Скачать на компьютер», «К исполнителю», «К альбому», «Удалить из медиатеки».
 */
public static class TrackActions
{
    /**
     * Подключить к ListView со списком Track. tracks — что играть (по умолчанию — элементы списка);
     * onDeleted — убрать удалённые из списка страницы.
     */
    public static void Attach(ListView list, Func<IReadOnlyList<Track>>? tracks = null, Action<IReadOnlyCollection<long>>? onDeleted = null,
        Action<MenuFlyout, IReadOnlyList<Track>>? extend = null)
    {
        IReadOnlyList<Track> All() => tracks?.Invoke() ?? list.Items.OfType<Track>().ToList();

        list.IsItemClickEnabled = false;
        list.SelectionMode = ListViewSelectionMode.Extended;
        list.DoubleTapped += (_, e) =>
        {
            if ((e.OriginalSource as FrameworkElement)?.DataContext is Track t) PlayFrom(All(), t);
        };
        list.KeyDown += (_, e) =>
        {
            if (e.Key == Windows.System.VirtualKey.Enter && list.SelectedItem is Track t)
            {
                PlayFrom(All(), t);
                e.Handled = true;
            }
        };
        list.ContextRequested += (_, e) =>
        {
            var t = (e.OriginalSource as FrameworkElement)?.DataContext as Track ?? list.SelectedItem as Track;
            if (t is null) return;
            var selected = list.SelectedItems.OfType<Track>().ToList();
            if (!selected.Contains(t)) selected = [t];
            var menu = Menu(list.XamlRoot, All(), t, selected, onDeleted, extend);
            if (e.TryGetPosition(e.OriginalSource as UIElement, out var p))
                menu.ShowAt(e.OriginalSource as FrameworkElement, new FlyoutShowOptions { Position = p });
            else
                menu.ShowAt(e.OriginalSource as FrameworkElement);
            e.Handled = true;
        };
    }

    public static void PlayFrom(IReadOnlyList<Track> all, Track t)
    {
        var i = IndexOf(all, t);
        PlayerService.Instance.Play(all, Math.Max(0, i));
    }

    private static int IndexOf(IReadOnlyList<Track> all, Track t)
    {
        for (var i = 0; i < all.Count; i++)
            if (ReferenceEquals(all[i], t) || all[i].Id == t.Id) return i;
        return -1;
    }

    public static MenuFlyout Menu(XamlRoot root, IReadOnlyList<Track> all, Track t, IReadOnlyList<Track> selected,
        Action<IReadOnlyCollection<long>>? onDeleted, Action<MenuFlyout, IReadOnlyList<Track>>? extend = null)
    {
        var p = PlayerService.Instance;
        var many = selected.Count > 1;
        var m = new MenuFlyout();
        m.Items.Add(Item(many ? $"Играть выбранные ({selected.Count})" : "Играть", "",
            () => { if (many) p.Play(selected); else PlayFrom(all, t); }));
        m.Items.Add(Item("Играть следующим", "", () => p.PlayNext(selected)));
        m.Items.Add(Item("Добавить в очередь", "", () => p.Enqueue(selected)));
        m.Items.Add(Item("Добавить в плейлист…", "", () => _ = PlaylistDialogs.AddToPlaylist(root, selected)));
        var off = OfflineDownloads.Instance;
        var notSaved = selected.Where(x => !off.IsDone(x.Id)).ToList();
        if (notSaved.Count > 0)
            m.Items.Add(Item(notSaved.Count > 1 ? $"Скачать на компьютер ({notSaved.Count})" : "Скачать на компьютер", "",
                () => SaveOffline(notSaved, null)));
        var saved = selected.Where(x => off.IsDone(x.Id)).ToList();
        if (saved.Count > 0)
            m.Items.Add(Item(saved.Count > 1 ? $"Удалить с компьютера ({saved.Count})" : "Удалить с компьютера", "",
                () => { foreach (var x in saved) off.Remove(x.Id); }));
        if (!many)
        {
            m.Items.Add(new MenuFlyoutSeparator());
            if (!string.IsNullOrEmpty(t.ArtistKey))
                m.Items.Add(Item("К исполнителю", "", () => App.Window?.Navigate(typeof(ArtistPage), t.ArtistKey)));
            if (!string.IsNullOrEmpty(t.ArtistKey) && !string.IsNullOrEmpty(t.AlbumKey))
                m.Items.Add(Item("К альбому", "", () => App.Window?.Navigate(typeof(AlbumPage), (t.ArtistKey!, t.AlbumKey!))));
        }
        var own = selected.Where(x => x.Owned).ToList();
        if (own.Count > 0)
        {
            m.Items.Add(new MenuFlyoutSeparator());
            m.Items.Add(Item(own.Count > 1 ? $"Удалить из медиатеки ({own.Count})" : "Удалить из медиатеки", "",
                () => _ = Delete(root, own, onDeleted)));
        }
        // пункты страницы (например, «Убрать из плейлиста»)
        extend?.Invoke(m, selected);
        return m;
    }

    /** «Скачать на компьютер»: в очередь офлайн-загрузок; group — альбом/плейлист для страницы «Скачанное». */
    public static void SaveOffline(IReadOnlyList<Track> tracks, string? group)
    {
        var n = OfflineDownloads.Instance.Add(tracks, group);
        if (n == 0) Ui.Info(tracks.Count == 1 ? "Трек уже скачан на компьютер" : "Эти треки уже скачаны на компьютер");
        else Ui.Success(n == 1 ? "Скачиваю на компьютер" : $"Скачиваю на компьютер: {Fmt.Tracks(n)}", "Скачанное",
            () => App.Window?.Navigate(typeof(OfflinePage)));
    }

    private static MenuFlyoutItem Item(string text, string glyph, Action click)
    {
        var i = new MenuFlyoutItem { Text = text, Icon = new FontIcon { Glyph = glyph } };
        i.Click += (_, _) => click();
        return i;
    }

    public static async Task Delete(XamlRoot root, IReadOnlyList<Track> tracks, Action<IReadOnlyCollection<long>>? onDeleted)
    {
        var text = tracks.Count == 1
            ? $"«{tracks[0].Title}» исчезнет из вашей медиатеки. В чужих медиатеках и общих плейлистах трек останется."
            : $"{Fmt.Tracks(tracks.Count)} исчезнут из вашей медиатеки.";
        if (!await Ui.Confirm(root, "Удалить из медиатеки?", text, "Удалить")) return;
        try
        {
            var r = await Api.Instance.Delete(tracks.Select(x => x.Id));
            PlayerService.Instance.RemoveGone(r.GoneIds);
            onDeleted?.Invoke(r.DeletedIds);
            Ui.Success(r.Deleted == 1 ? "Трек удалён" : $"Удалено: {Fmt.Tracks(r.Deleted)}");
        }
        catch (Exception e) { Ui.Error(e); }
    }
}
