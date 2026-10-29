using System.Text.RegularExpressions;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using PiMusic.Data;

namespace PiMusic.Views;

/**
 * Диалоги плейлистов: «В плейлисты» (отметки по всем плейлистам), «Новый плейлист», переименование, участники,
 * удаление/выход и вступление по ссылке-приглашению.
 */
public static partial class PlaylistDialogs
{
    /** Сколько id отправлять в одном запросе (лимит сервера — 500). */
    private const int Chunk = 500;
    private const int MaxName = 100;
    private const int MaxDesc = 300;

    [GeneratedRegex(@"(?:#join=|[?&]start=pl_)([A-Za-z0-9_-]{6,64})")]
    private static partial Regex InviteInLink();

    [GeneratedRegex(@"^[A-Za-z0-9_-]{6,64}$")]
    private static partial Regex BareToken();

    /** Токен приглашения из «…#join=&lt;t&gt;», «…?start=pl_&lt;t&gt;» или голого токена; null — не похоже на приглашение. */
    public static string? InviteToken(string? text)
    {
        var s = text?.Trim() ?? "";
        if (s.Length == 0) return null;
        var m = InviteInLink().Match(s);
        if (m.Success) return m.Groups[1].Value;
        return BareToken().IsMatch(s) ? s : null;
    }

    private static ContentDialog Dialog(XamlRoot root, string title, object content, string primary) => new()
    {
        XamlRoot = root,
        Title = title,
        Content = content,
        PrimaryButtonText = primary,
        CloseButtonText = "Отмена",
        DefaultButton = ContentDialogButton.Primary,
        Style = (Style)Application.Current.Resources["DefaultContentDialogStyle"],
    };

    private static TextBlock Secondary(string text) => new()
    {
        Text = text,
        Style = (Style)Application.Current.Resources["CaptionTextBlockStyle"],
        Foreground = (Brush)Application.Current.Resources["TextFillColorSecondaryBrush"],
        TextWrapping = TextWrapping.Wrap,
    };

    // ---------------------------------------------------------------- «В плейлисты»

    /** Строка листа: плейлист, сколько выбранных треков в нём уже есть, флажок. */
    private sealed class Mark(Playlist p, int have, CheckBox box)
    {
        public Playlist Playlist { get; } = p;
        public int Have { get; } = have;
        public CheckBox Box { get; } = box;
        public bool? Initial { get; init; }
    }

    /**
     * Лист «В плейлисты» для выбранных треков: сверху «Новый плейлист», ниже все плейлисты с флажками. Для ≤ 50 треков
     * начальные отметки — по for-track (все/часть/нет); «Готово» разом добавляет в отмеченные и убирает из снятых.
     */
    public static async Task AddToPlaylist(XamlRoot root, IReadOnlyList<Track> tracks)
    {
        var tids = tracks.Select(t => t.Id).Distinct().ToList();
        if (tids.Count == 0) return;
        var marks = new List<Mark>();
        var panel = new StackPanel { Spacing = 4, MinWidth = 360 };
        var create = new Button
        {
            Style = (Style)Application.Current.Resources["SubtleButtonStyle"],
            HorizontalAlignment = HorizontalAlignment.Stretch,
            HorizontalContentAlignment = HorizontalAlignment.Left,
            Content = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                Spacing = 12,
                Children = { new FontIcon { Glyph = "", FontSize = 16 }, new TextBlock { Text = "Новый плейлист" } },
            },
        };
        var list = new StackPanel();
        var busy = new ProgressRing { IsActive = true, Margin = new Thickness(0, 16, 0, 16) };
        panel.Children.Add(create);
        panel.Children.Add(busy);
        panel.Children.Add(new ScrollViewer { Content = list, MaxHeight = 360 });

        var title = tids.Count == 1 ? "В плейлисты" : $"В плейлисты · {Fmt.Tracks(tids.Count)}";
        var d = Dialog(root, title, panel, "Готово");
        d.IsPrimaryButtonEnabled = false;
        var wantNew = false;
        create.Click += (_, _) => { wantNew = true; d.Hide(); };

        void Changed() => d.IsPrimaryButtonEnabled = marks.Any(m => m.Box.IsChecked != m.Initial);

        d.Opened += async (_, _) =>
        {
            try
            {
                var all = (await Api.Instance.Playlists()).Items.Where(p => p.Role is "owner" or "member").ToList();
                var have = new Dictionary<long, int>();
                if (tids.Count <= 50)
                    foreach (var tid in tids)
                        foreach (var it in (await Api.Instance.PlaylistsForTrack(tid)).Items.Where(i => i.Has))
                            have[it.Id] = have.GetValueOrDefault(it.Id) + 1;
                foreach (var p in all)
                {
                    var n = have.GetValueOrDefault(p.Id);
                    bool? state = n == 0 ? false : n >= tids.Count ? true : null;
                    // IsThreeState=false: «часть» (null) показывается, но щелчок переключает только «да/нет».
                    var box = new CheckBox { Content = p.Name, IsThreeState = false, IsChecked = state, MinHeight = 40 };
                    ToolTipService.SetToolTip(box, p.Subtitle);
                    box.Checked += (_, _) => Changed();
                    box.Unchecked += (_, _) => Changed();
                    marks.Add(new Mark(p, n, box) { Initial = state });
                    list.Children.Add(box);
                }
                if (all.Count == 0) list.Children.Add(Secondary("Плейлистов пока нет — создайте первый."));
            }
            catch (Exception ex) { Ui.Error(ex); }
            finally { busy.IsActive = false; busy.Visibility = Visibility.Collapsed; }
        };

        var r = await d.ShowAsync();
        if (wantNew) { await NewPlaylist(root, tracks); return; }
        if (r != ContentDialogResult.Primary) return;

        int added = 0, removed = 0;
        var full = false;
        try
        {
            foreach (var m in marks.Where(m => m.Box.IsChecked != m.Initial))
            {
                if (m.Box.IsChecked == true && m.Have < tids.Count)
                {
                    foreach (var chunk in tids.Chunk(Chunk))
                    {
                        var a = await Api.Instance.PlaylistAdd(m.Playlist.Id, chunk);
                        added += a.Added;
                        if (a.Full) { full = true; break; }
                    }
                }
                else if (m.Box.IsChecked == false && m.Have > 0)
                {
                    var set = tids.ToHashSet();
                    var items = ((await Api.Instance.Playlist(m.Playlist.Id)).Tracks ?? [])
                        .Where(t => set.Contains(t.Id) && t.ItemId is not null).Select(t => t.ItemId!.Value).ToList();
                    foreach (var chunk in items.Chunk(Chunk))
                        removed += (await Api.Instance.PlaylistRemove(m.Playlist.Id, chunk)).Removed;
                }
            }
        }
        catch (Exception ex) { Ui.Error(ex); return; }
        var parts = new List<string>();
        if (added > 0) parts.Add($"Добавлено: {added}");
        if (removed > 0) parts.Add(parts.Count > 0 ? $"убрано: {removed}" : $"Убрано: {removed}");
        if (full) parts.Add(parts.Count > 0 ? "плейлист заполнен" : "Плейлист заполнен");
        var text = parts.Count > 0 ? string.Join(", ", parts) : "Без изменений";
        if (full) App.Window?.Toast(text, InfoBarSeverity.Warning);
        else Ui.Success(text);
    }

    // ---------------------------------------------------------------- создание и переименование

    /** Поля «Название (счётчик) / Описание / Общий»; showShared=false — без переключателя (переименование). */
    private static async Task<(string Name, string Description, bool Shared)?> NameDialog(XamlRoot root, string title, string action,
        string name = "", string description = "", bool showShared = true)
    {
        var nameBox = new TextBox { Header = "Название", Text = name, MaxLength = MaxName, PlaceholderText = "Например, «В дорогу»" };
        var counter = Secondary("");
        counter.HorizontalAlignment = HorizontalAlignment.Right;
        var descBox = new TextBox
        {
            Header = "Описание",
            Text = description,
            MaxLength = MaxDesc,
            AcceptsReturn = true,
            TextWrapping = TextWrapping.Wrap,
            MinHeight = 72,
            PlaceholderText = "Необязательно",
        };
        var shared = new ToggleSwitch { OnContent = "", OffContent = "", MinWidth = 0, VerticalAlignment = VerticalAlignment.Center };
        var panel = new StackPanel { Spacing = 8, MinWidth = 360 };
        panel.Children.Add(nameBox);
        panel.Children.Add(counter);
        panel.Children.Add(descBox);
        if (showShared)
        {
            var row = new Grid { ColumnSpacing = 12, Margin = new Thickness(0, 8, 0, 0) };
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var text = new StackPanel();
            text.Children.Add(new TextBlock { Text = "Общий плейлист" });
            text.Children.Add(Secondary("Друзья смогут добавлять треки по ссылке"));
            row.Children.Add(text);
            Grid.SetColumn(shared, 1);
            row.Children.Add(shared);
            panel.Children.Add(row);
        }
        var d = Dialog(root, title, panel, action);
        void Update()
        {
            counter.Text = $"{nameBox.Text.Length}/{MaxName}";
            d.IsPrimaryButtonEnabled = nameBox.Text.Trim().Length > 0;
        }
        nameBox.TextChanged += (_, _) => Update();
        Update();
        d.Opened += (_, _) => { nameBox.Focus(FocusState.Programmatic); nameBox.SelectAll(); };
        if (await d.ShowAsync() != ContentDialogResult.Primary) return null;
        return (nameBox.Text.Trim(), descBox.Text.Trim(), shared.IsOn);
    }

    /**
     * Диалог «Новый плейлист»; initialTracks — сразу добавить эти треки. Возвращает созданный плейлист (или null).
     * Без треков вызывающий сам переходит на плейлист; с треками — только сообщение.
     */
    public static async Task<Playlist?> NewPlaylist(XamlRoot root, IReadOnlyList<Track>? initialTracks = null)
    {
        if (await NameDialog(root, "Новый плейлист", "Создать") is not { } v) return null;
        try
        {
            var ids = initialTracks?.Select(t => t.Id).Distinct().ToList();
            var first = ids?.Take(Chunk).ToList();
            var p = await Api.Instance.CreatePlaylist(v.Name, v.Description.Length > 0 ? v.Description : null, v.Shared,
                first is { Count: > 0 } ? first : null);
            var added = p.AddedNow;
            if (ids is not null)
                foreach (var chunk in ids.Skip(Chunk).Chunk(Chunk))
                {
                    var a = await Api.Instance.PlaylistAdd(p.Id, chunk);
                    added += a.Added;
                    if (a.Full) break;
                }
            var id = p.Id;
            Ui.Success(ids is { Count: > 0 } ? $"Плейлист «{p.Name}» создан, добавлено: {added}" : $"Плейлист «{p.Name}» создан",
                "Открыть", () => App.Window?.Navigate(typeof(PlaylistPage), id));
            return p;
        }
        catch (Exception ex) { Ui.Error(ex); return null; }
    }

    /** Переименовать (название и описание); true — сохранено. */
    public static async Task<bool> Rename(XamlRoot root, Playlist p)
    {
        if (await NameDialog(root, "Переименовать", "Сохранить", p.Name, p.Description, showShared: false) is not { } v) return false;
        try
        {
            await Api.Instance.UpdatePlaylist(p.Id, v.Name, v.Description);
            return true;
        }
        catch (Exception ex) { Ui.Error(ex); return false; }
    }

    // ---------------------------------------------------------------- удаление, выход, участники

    /** Удалить плейлист (владелец) с подтверждением; true — удалён. */
    public static async Task<bool> Delete(XamlRoot root, Playlist p)
    {
        if (!await Ui.Confirm(root, "Удалить плейлист?", $"«{p.Name}» удалится у вас и у участников. Треки останутся в медиатеке.", "Удалить"))
            return false;
        try
        {
            await Api.Instance.DeletePlaylist(p.Id);
            Ui.Success("Плейлист удалён");
            return true;
        }
        catch (Exception ex) { Ui.Error(ex); return false; }
    }

    /** Выйти из чужого плейлиста с подтверждением; true — вышли. */
    public static async Task<bool> Leave(XamlRoot root, Playlist p)
    {
        if (!await Ui.Confirm(root, "Выйти из плейлиста?", "Вернуться можно будет только по новой ссылке-приглашению.", "Выйти"))
            return false;
        try
        {
            await Api.Instance.LeavePlaylist(p.Id);
            Ui.Success($"Вы вышли из «{p.Name}»");
            return true;
        }
        catch (Exception ex) { Ui.Error(ex); return false; }
    }

    private static PersonPicture Avatar(string login) => new() { DisplayName = login, Width = 32, Height = 32 };

    /** Участники: владелец и приглашённые; владельцу — «Убрать». true — кого-то убрали (перезагрузить страницу). */
    public static async Task<bool> Members(XamlRoot root, Playlist p)
    {
        var changed = false;
        var list = new StackPanel { Spacing = 4, MinWidth = 360 };

        Grid Row(string login, string caption, bool kick)
        {
            var g = new Grid { ColumnSpacing = 12, MinHeight = 48 };
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            g.Children.Add(Avatar(login));
            var text = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
            text.Children.Add(new TextBlock { Text = login, TextTrimming = TextTrimming.CharacterEllipsis });
            if (caption.Length > 0) text.Children.Add(Secondary(caption));
            Grid.SetColumn(text, 1);
            g.Children.Add(text);
            if (kick)
            {
                var b = new Button { Content = "Убрать", VerticalAlignment = VerticalAlignment.Center };
                AutomationProperties(b, $"Убрать {login}");
                b.Click += async (_, _) =>
                {
                    b.IsEnabled = false;
                    try
                    {
                        await Api.Instance.KickMember(p.Id, login);
                        list.Children.Remove(g);
                        changed = true;
                        Ui.Success($"{login} больше не участник");
                    }
                    catch (Exception ex) { b.IsEnabled = true; Ui.Error(ex); }
                };
                Grid.SetColumn(b, 2);
                g.Children.Add(b);
            }
            return g;
        }

        list.Children.Add(Row(p.Owner, "владелец", false));
        foreach (var m in p.Members) list.Children.Add(Row(m.Login, "", p.IsOwner));
        if (p.Members.Count == 0)
            list.Children.Add(Secondary(p.IsOwner ? "Пока никого. Поделитесь ссылкой — друзья смогут присоединиться." : "Пока никого."));
        var d = new ContentDialog
        {
            XamlRoot = root,
            Title = "Участники",
            Content = new ScrollViewer { Content = list, MaxHeight = 420 },
            CloseButtonText = "Закрыть",
            DefaultButton = ContentDialogButton.Close,
            Style = (Style)Application.Current.Resources["DefaultContentDialogStyle"],
        };
        await d.ShowAsync();
        return changed;
    }

    private static void AutomationProperties(UIElement e, string name) =>
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(e, name);

    // ---------------------------------------------------------------- вступление по ссылке

    /**
     * Вступление по ссылке-приглашению: поле для «…#join=&lt;t&gt;», «…?start=pl_&lt;t&gt;» или токена, превью плейлиста,
     * «Вступить» (или «Открыть», если вы уже в нём) → переход на плейлист. linkOrToken — заполнить поле заранее.
     */
    public static async Task Join(XamlRoot root, string? linkOrToken)
    {
        var box = new TextBox { Header = "Ссылка-приглашение", PlaceholderText = "https://…#join=… или код", Text = linkOrToken ?? "" };
        var mosaic = new PlaylistMosaic { Width = 72, Height = 72, DecodeSize = 72 };
        var name = new TextBlock
        {
            Style = (Style)Application.Current.Resources["BodyStrongTextBlockStyle"],
            TextTrimming = TextTrimming.CharacterEllipsis,
        };
        var info = Secondary("");
        var note = Secondary("");
        var texts = new StackPanel { VerticalAlignment = VerticalAlignment.Center, Spacing = 2, Children = { name, info, note } };
        var card = new Grid
        {
            ColumnSpacing = 12,
            Padding = new Thickness(12),
            CornerRadius = new CornerRadius(8),
            Background = (Brush)Application.Current.Resources["CardBackgroundFillColorDefaultBrush"],
            Visibility = Visibility.Collapsed,
        };
        card.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        card.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        card.Children.Add(mosaic);
        Grid.SetColumn(texts, 1);
        card.Children.Add(texts);
        var status = Secondary("");
        var busy = new ProgressRing { IsActive = false, Width = 20, Height = 20, HorizontalAlignment = HorizontalAlignment.Left };
        var panel = new StackPanel { Spacing = 12, MinWidth = 380, Children = { box, busy, status, card } };

        var d = Dialog(root, "Вступить по ссылке", panel, "Вступить");
        d.IsPrimaryButtonEnabled = false;
        JoinPreview? preview = null;
        string? token = null;
        var version = 0;

        async Task Check()
        {
            var t = InviteToken(box.Text);
            if (t == token) return;
            token = t;
            preview = null;
            card.Visibility = Visibility.Collapsed;
            d.IsPrimaryButtonEnabled = false;
            d.PrimaryButtonText = "Вступить";
            status.Text = t is null && box.Text.Trim().Length > 0 ? "Это не похоже на ссылку-приглашение" : "";
            if (t is null) return;
            var my = ++version;
            busy.IsActive = true;
            try
            {
                var pv = await Api.Instance.JoinPreview(t);
                if (my != version) return;
                preview = pv;
                mosaic.Covers = pv.CoverUris;
                name.Text = pv.Name;
                info.Text = Fmt.Join($"от {pv.Owner}", Fmt.Tracks(pv.Count));
                note.Text = pv.Already ? "Вы уже в этом плейлисте" : "";
                note.Visibility = pv.Already ? Visibility.Visible : Visibility.Collapsed;
                card.Visibility = Visibility.Visible;
                d.PrimaryButtonText = pv.Already ? "Открыть" : "Вступить";
                d.IsPrimaryButtonEnabled = true;
            }
            catch (Exception ex)
            {
                if (my != version) return;
                status.Text = ex is ApiException ? ex.Message : "Не удалось проверить ссылку: " + ex.Message;
            }
            finally { if (my == version) busy.IsActive = false; }
        }

        box.TextChanged += (_, _) => _ = Check();
        d.Opened += (_, _) => { box.Focus(FocusState.Programmatic); _ = Check(); };
        if (await d.ShowAsync() != ContentDialogResult.Primary || preview is null || token is null) return;
        if (preview.Already)
        {
            App.Window?.Navigate(typeof(PlaylistPage), preview.Id);
            return;
        }
        try
        {
            var p = await Api.Instance.JoinPlaylist(token);
            Ui.Success($"Вы в плейлисте «{p.Name}»");
            App.Window?.Navigate(typeof(PlaylistPage), p.Id);
        }
        catch (Exception ex) { Ui.Error(ex); }
    }
}
