using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using PiMusic.Data;

namespace PiMusic.Views;

/**
 * Карточка need_info: сервер не опознал файл — спросить исполнителя, название и альбом.
 * Собирается в коде и живёт, пока загрузка ждёт ответа: опрос списка не сбрасывает введённое.
 */
public sealed partial class UploadInfoCard : Grid
{
    private readonly TextBox _artist, _title, _album;
    private readonly Button _save;
    private readonly Action _changed;

    public long Id { get; }

    public UploadInfoCard(Upload u, Action changed)
    {
        Id = u.Id;
        _changed = changed;
        Padding = new Thickness(16);
        CornerRadius = new CornerRadius(8);
        RowSpacing = 12;
        BorderThickness = new Thickness(1);
        Background = Res<Brush>("CardBackgroundFillColorDefaultBrush");
        BorderBrush = Res<Brush>("CardStrokeColorDefaultBrush");

        var (gArtist, gTitle) = Guess(u.Name);
        var head = new StackPanel();
        head.Children.Add(new TextBlock
        {
            Text = u.Duration is > 0 ? $"{u.Name} · {Fmt.Time(TimeSpan.FromSeconds(u.Duration.Value))}" : u.Name,
            Style = Res<Style>("BodyStrongTextBlockStyle"),
            TextTrimming = TextTrimming.CharacterEllipsis,
        });
        head.Children.Add(new TextBlock
        {
            Text = "Не удалось опознать — подскажите, что это за трек.",
            TextWrapping = TextWrapping.Wrap,
            Foreground = Res<Brush>("TextFillColorSecondaryBrush"),
        });

        _artist = Field("Исполнитель", string.IsNullOrEmpty(u.Artist) ? gArtist : u.Artist);
        _title = Field("Название", string.IsNullOrEmpty(u.Title) ? gTitle : u.Title);
        _album = Field("Альбом (необязательно)", u.Album ?? "");
        var fields = new Grid { ColumnSpacing = 8 };
        for (var i = 0; i < 3; i++) fields.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        fields.Children.Add(_artist);
        fields.Children.Add(_title); SetColumn(_title, 1);
        fields.Children.Add(_album); SetColumn(_album, 2);

        _save = new Button { Content = "Сохранить", Style = Res<Style>("AccentButtonStyle") };
        _save.Click += Save_Click;
        var skip = new Button { Content = "Оставить как есть" };
        skip.Click += async (_, _) => await Act(() => Api.Instance.UploadSkip(Id), null);
        var cancel = new Button { Content = "Отмена" };
        cancel.Click += async (_, _) => await Act(() => Api.Instance.UploadCancel(Id), null);
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, HorizontalAlignment = HorizontalAlignment.Right };
        buttons.Children.Add(cancel);
        buttons.Children.Add(skip);
        buttons.Children.Add(_save);

        for (var i = 0; i < 3; i++) RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        Children.Add(head);
        Children.Add(fields); SetRow(fields, 1);
        Children.Add(buttons); SetRow(buttons, 2);
        Validate();
    }

    /** «Артист - Название.mp3» → (артист, название); без разделителя — только название. */
    public static (string Artist, string Title) Guess(string fileName)
    {
        var stem = Path.GetFileNameWithoutExtension(fileName).Replace('_', ' ').Trim();
        foreach (var sep in new[] { " - ", " — ", " – " })
        {
            var i = stem.IndexOf(sep, StringComparison.Ordinal);
            if (i > 0) return (stem[..i].Trim(), stem[(i + sep.Length)..].Trim());
        }
        return ("", stem);
    }

    private TextBox Field(string header, string text)
    {
        var t = new TextBox { Header = header, Text = text, IsSpellCheckEnabled = false };
        t.TextChanged += (_, _) => Validate();
        return t;
    }

    private void Validate()
    {
        if (_save is not null) _save.IsEnabled = _artist.Text.Trim().Length > 0 && _title.Text.Trim().Length > 0;
    }

    private async void Save_Click(object sender, RoutedEventArgs e)
    {
        var artist = _artist.Text.Trim();
        var title = _title.Text.Trim();
        var album = _album.Text.Trim();
        await Act(() => Api.Instance.UploadInfo(Id, artist, title, album.Length > 0 ? album : null), $"Сохраняю «{artist} — {title}»");
    }

    private async Task Act(Func<Task<Upload>> call, string? done)
    {
        IsHitTestVisible = false;
        Opacity = 0.6;
        try
        {
            await call();
            if (done is not null) Ui.Success(done);
            _changed();
        }
        catch (Exception ex)
        {
            Ui.Error(ex);
            IsHitTestVisible = true;
            Opacity = 1;
        }
    }

    private static T Res<T>(string key) => (T)Application.Current.Resources[key];
}
