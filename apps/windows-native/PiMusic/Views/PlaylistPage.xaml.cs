using System.Collections.ObjectModel;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Navigation;
using PiMusic.Data;
using PiMusic.Player;
using Windows.ApplicationModel.DataTransfer;

namespace PiMusic.Views;

/**
 * Плейлист. Параметр — id (long). Шапка (мозаика, имя, описание, «Общий», владелец/участники, длительность),
 * «Слушать/Перемешать», сортировка сервера (запоминается на время работы приложения), меню «…»; при порядке
 * плейлиста — перетаскивание строк и «Переместить…» в контекстном меню.
 */
public sealed partial class PlaylistPage : Page
{
    /** Варианты сортировки: ключ, подпись, направление по умолчанию. */
    private static readonly (string Key, string Label, string Order)[] Sorts =
    [
        ("manual", "Порядок плейлиста", "asc"), ("added", "Дата добавления", "desc"), ("title", "Название", "asc"),
        ("artist", "Исполнитель", "asc"), ("album", "Альбом", "asc"), ("genre", "Жанр", "asc"), ("year", "Год", "desc"),
        ("duration", "Длительность", "asc"), ("random", "Случайный порядок", "asc"),
    ];

    /** Сортировка каждого плейлиста — в памяти процесса. */
    private static readonly Dictionary<long, (string Sort, string Order, long Seed)> Remembered = [];

    private long _id;
    private Playlist? _pl;
    private ObservableCollection<Track> _tracks = [];
    private (string Sort, string Order, long Seed) _sort = ("manual", "asc", 0);
    private bool _ready;

    public PlaylistPage()
    {
        InitializeComponent();
        foreach (var s in Sorts) SortBox.Items.Add(new ComboBoxItem { Content = s.Label, Tag = s.Key });
        TrackActions.Attach(List, () => [.. _tracks], onDeleted: ids => _ = Load(), extend: Extend);
    }

    private bool Manual => _sort.Sort == "manual";
    private static bool NoDir(string sort) => sort is "manual" or "random";

    protected override void OnNavigatedTo(NavigationEventArgs e)
    {
        if (e.Parameter is long id) _id = id;
        if (Remembered.TryGetValue(_id, out var s)) _sort = s;
        SortBox.SelectedIndex = Math.Max(0, Array.FindIndex(Sorts, x => x.Key == _sort.Sort));
        UpdateSortUi();
        _ready = true;
        _ = Load();
    }

    private async Task Load()
    {
        Busy.IsActive = true;
        try
        {
            var p = await Api.Instance.Playlist(_id, _sort.Sort, NoDir(_sort.Sort) ? null : _sort.Order,
                _sort.Sort == "random" ? _sort.Seed : null);
            _pl = p;
            var tracks = p.Tracks ?? [];
            TitleText.Text = p.Name;
            DescText.Text = p.Description;
            DescText.Visibility = string.IsNullOrWhiteSpace(p.Description) ? Visibility.Collapsed : Visibility.Visible;
            SharedChip.Visibility = p.Shared ? Visibility.Visible : Visibility.Collapsed;
            Mosaic.Covers = p.CoverUris;
            var members = p.Members.Count == 0 ? null
                : "участники: " + string.Join(", ", p.Members.Take(3).Select(m => m.Login)) + (p.Members.Count > 3 ? $" и ещё {p.Members.Count - 3}" : "");
            var seconds = p.Seconds > 0 ? p.Seconds : tracks.Sum(t => t.Seconds);
            InfoText.Text = Ui.Join(p.IsOwner ? null : $"от {p.Owner}", members, Fmt.Tracks(tracks.Count),
                seconds > 0 ? Fmt.Time(TimeSpan.FromSeconds(seconds)) : null);
            NotOwnedBar.IsOpen = tracks.Any(t => !t.Owned);
            PlayButton.IsEnabled = ShuffleButton.IsEnabled = tracks.Count > 0;
            Empty.Visibility = tracks.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
            _tracks = new ObservableCollection<Track>(tracks);
            List.ItemsSource = _tracks;
            UpdateSortUi();
        }
        catch (Exception ex) { Ui.Error(ex); }
        finally { Busy.IsActive = false; }
    }

    // ---------------------------------------------------------------- сортировка

    private void UpdateSortUi()
    {
        var manual = Manual;
        List.CanReorderItems = manual;
        List.CanDragItems = manual;
        List.AllowDrop = manual;
        HintText.Visibility = manual && _tracks.Count > 1 ? Visibility.Visible : Visibility.Collapsed;
        OrderButton.Visibility = NoDir(_sort.Sort) ? Visibility.Collapsed : Visibility.Visible;
        var asc = _sort.Order == "asc";
        OrderIcon.Glyph = asc ? "" : "";
        ToolTipService.SetToolTip(OrderButton, asc ? "По возрастанию" : "По убыванию");
    }

    private void SortBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!_ready || SortBox.SelectedItem is not ComboBoxItem { Tag: string key }) return;
        if (key == _sort.Sort && key != "random") return;
        var def = Sorts.First(s => s.Key == key);
        _sort = (key, def.Order, key == "random" ? Random.Shared.NextInt64(1, 1_000_000) : 0);
        Remembered[_id] = _sort;
        UpdateSortUi();
        _ = Load();
    }

    private void Order_Click(object sender, RoutedEventArgs e)
    {
        _sort = _sort with { Order = _sort.Order == "asc" ? "desc" : "asc" };
        Remembered[_id] = _sort;
        UpdateSortUi();
        _ = Load();
    }

    // ---------------------------------------------------------------- воспроизведение

    private void Play_Click(object sender, RoutedEventArgs e)
    {
        if (_tracks.Count > 0) PlayerService.Instance.Play([.. _tracks]);
    }

    private void Shuffle_Click(object sender, RoutedEventArgs e)
    {
        if (_tracks.Count > 0) PlayerService.Instance.Play([.. _tracks], 0, shuffle: true);
    }

    // ---------------------------------------------------------------- контекстное меню трека: убрать, переместить

    private void Extend(MenuFlyout m, IReadOnlyList<Track> selected)
    {
        var items = selected.Where(t => t.ItemId is not null).ToList();
        if (items.Count == 0 || _pl is null) return;
        m.Items.Add(new MenuFlyoutSeparator());
        m.Items.Add(Item(items.Count > 1 ? $"Убрать из плейлиста ({items.Count})" : "Убрать из плейлиста", "", () => _ = Remove(items)));
        if (!Manual) return;
        if (items.Count == 1)
        {
            var t = items[0];
            var i = _tracks.IndexOf(t);
            var move = new MenuFlyoutSubItem { Text = "Переместить", Icon = new FontIcon { Glyph = "" } };
            if (i > 0)
            {
                move.Items.Add(Item("Выше", "", () => _ = Move(t, i - 1)));
                move.Items.Add(Item("В начало", "", () => _ = Move(t, 0)));
            }
            if (i >= 0 && i < _tracks.Count - 1)
            {
                move.Items.Add(Item("Ниже", "", () => _ = Move(t, i + 1)));
                move.Items.Add(Item("В конец", "", () => _ = Move(t, _tracks.Count - 1)));
            }
            if (move.Items.Count > 0) m.Items.Add(move);
        }
        else
        {
            var ordered = items.OrderBy(_tracks.IndexOf).ToList();
            var move = new MenuFlyoutSubItem { Text = "Переместить", Icon = new FontIcon { Glyph = "" } };
            move.Items.Add(Item("В начало", "", () => _ = MoveMany(ordered, 0)));
            move.Items.Add(Item("В конец", "", () => _ = MoveMany(ordered, _tracks.Count)));
            m.Items.Add(move);
        }
    }

    private static MenuFlyoutItem Item(string text, string glyph, Action click)
    {
        var i = new MenuFlyoutItem { Text = text, Icon = new FontIcon { Glyph = glyph } };
        i.Click += (_, _) => click();
        return i;
    }

    private async Task Remove(IReadOnlyList<Track> items)
    {
        try
        {
            var removed = 0;
            foreach (var chunk in items.Select(t => t.ItemId!.Value).Chunk(500))
                removed += (await Api.Instance.PlaylistRemove(_id, chunk)).Removed;
            Ui.Success($"Убрано из плейлиста: {removed}");
        }
        catch (Exception ex) { Ui.Error(ex); }
        await Load();
    }

    /** Одна позиция на индекс to итогового списка; список на экране обновляется сразу. */
    private async Task Move(Track t, int to)
    {
        var from = _tracks.IndexOf(t);
        if (from < 0 || t.ItemId is null) return;
        to = Math.Clamp(to, 0, _tracks.Count - 1);
        if (from != to) _tracks.Move(from, to);
        try { await Api.Instance.PlaylistMove(_id, t.ItemId.Value, to); }
        catch (Exception ex) { Ui.Error(ex); await Load(); }
    }

    /** Выбранные позиции — блоком на индекс to (среди остальных), порядок внутри блока сохраняется. */
    private async Task MoveMany(IReadOnlyList<Track> ordered, int to)
    {
        try
        {
            await Api.Instance.PlaylistMoveMany(_id, ordered.Select(t => t.ItemId!.Value), to);
            Ui.Success($"Перемещено: {Fmt.Tracks(ordered.Count)}");
        }
        catch (Exception ex) { Ui.Error(ex); }
        await Load();
    }

    /** Перетаскивание мышью (только при порядке плейлиста): ListView уже переставил строки — сообщаем серверу. */
    private async void List_DragItemsCompleted(ListViewBase sender, DragItemsCompletedEventArgs args)
    {
        if (!Manual || args.DropResult != DataPackageOperation.Move) return;
        var moved = args.Items.OfType<Track>().Where(t => t.ItemId is not null)
            .Select(t => (Track: t, Index: _tracks.IndexOf(t))).Where(x => x.Index >= 0).OrderBy(x => x.Index).ToList();
        if (moved.Count == 0) return;
        try
        {
            if (moved.Count == 1) await Api.Instance.PlaylistMove(_id, moved[0].Track.ItemId!.Value, moved[0].Index);
            else await Api.Instance.PlaylistMoveMany(_id, moved.Select(x => x.Track.ItemId!.Value), moved[0].Index);
        }
        catch (Exception ex) { Ui.Error(ex); await Load(); }
    }

    // ---------------------------------------------------------------- меню «…»

    private void More_Click(object sender, RoutedEventArgs e)
    {
        if (_pl is not { } p) return;
        var m = new MenuFlyout();
        m.Items.Add(Item("Переименовать", "", () => _ = Rename()));
        if (p.IsOwner)
        {
            m.Items.Add(Item(p.InviteEnabled ? "Скопировать ссылку-приглашение" : "Поделиться", "", () => _ = Share(false)));
            if (p.InviteEnabled)
            {
                m.Items.Add(Item("Новая ссылка", "", () => _ = Share(true)));
                m.Items.Add(Item("Выключить ссылку", "", () => _ = Unshare()));
            }
        }
        m.Items.Add(Item("Участники", "", () => Members_Click(this, new RoutedEventArgs())));
        m.Items.Add(Item("Сохранить в медиатеку", "", () => Save_Click(this, new RoutedEventArgs())));
        if (_tracks.Count > 0)
            m.Items.Add(Item("Скачать на компьютер", "", () => TrackActions.SaveOffline([.. _tracks], p.Name)));
        m.Items.Add(new MenuFlyoutSeparator());
        if (p.IsOwner) m.Items.Add(Item("Удалить плейлист", "", () => _ = Delete()));
        else m.Items.Add(Item("Выйти из плейлиста", "", () => _ = Leave()));
        m.ShowAt(MoreButton);
    }

    private async Task Rename()
    {
        if (_pl is not null && await PlaylistDialogs.Rename(XamlRoot, _pl)) await Load();
    }

    /** Включить ссылку (regenerate — новую) и скопировать её в буфер обмена. */
    private async Task Share(bool regenerate)
    {
        if (_pl is not { } p) return;
        if (regenerate && !await Ui.Confirm(XamlRoot, "Новая ссылка?", "Старая ссылка-приглашение перестанет работать.", "Создать"))
            return;
        try
        {
            var inv = !regenerate && p.InviteEnabled ? p.Invite : null;
            if (inv is null)
            {
                var r = await Api.Instance.SharePlaylist(p.Id, true, regenerate);
                inv = r.Invite;
            }
            var link = inv?.AppLink ?? inv?.Link;
            if (string.IsNullOrEmpty(link)) { Ui.Info("Не удалось получить ссылку"); return; }
            var data = new DataPackage();
            data.SetText(link);
            Clipboard.SetContent(data);
            Ui.Success("Ссылка скопирована");
        }
        catch (Exception ex) { Ui.Error(ex); }
        await Load();
    }

    private async Task Unshare()
    {
        if (_pl is not { } p) return;
        try
        {
            await Api.Instance.SharePlaylist(p.Id, false);
            Ui.Success("Ссылка выключена");
        }
        catch (Exception ex) { Ui.Error(ex); }
        await Load();
    }

    private async void Members_Click(object sender, RoutedEventArgs e)
    {
        if (_pl is not null && await PlaylistDialogs.Members(XamlRoot, _pl)) await Load();
    }

    private async void Save_Click(object sender, RoutedEventArgs e)
    {
        if (_pl is not { } p) return;
        try
        {
            var r = await Api.Instance.SavePlaylist(p.Id);
            Ui.Success(r.Linked > 0 ? $"В медиатеку добавлено: {r.Linked}" : "Все треки уже в медиатеке");
        }
        catch (Exception ex) { Ui.Error(ex); }
        await Load();
    }

    private async Task Delete()
    {
        if (_pl is not null && await PlaylistDialogs.Delete(XamlRoot, _pl)) Back();
    }

    private async Task Leave()
    {
        if (_pl is not null && await PlaylistDialogs.Leave(XamlRoot, _pl)) Back();
    }

    private void Back()
    {
        if (Frame.CanGoBack) Frame.GoBack();
        else App.Window?.Navigate(typeof(PlaylistsPage));
    }
}
