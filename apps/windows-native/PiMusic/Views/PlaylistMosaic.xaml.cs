using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace PiMusic.Views;

/** Мозаика обложек плейлиста (до 4). Размер задаёт Width/Height, DecodeSize — px для декодирования картинок. */
public sealed partial class PlaylistMosaic : UserControl
{
    public static readonly DependencyProperty CoversProperty = DependencyProperty.Register(
        nameof(Covers), typeof(object), typeof(PlaylistMosaic), new PropertyMetadata(null, (d, _) => ((PlaylistMosaic)d).Update()));

    public PlaylistMosaic()
    {
        InitializeComponent();
        SizeChanged += (_, e) => Placeholder.FontSize = Math.Max(16, Math.Round(e.NewSize.Width / 4));
    }

    /** Абсолютные ссылки на обложки (List&lt;Uri&gt;). */
    public object? Covers
    {
        get => GetValue(CoversProperty);
        set => SetValue(CoversProperty, value);
    }

    /** Размер стороны в px для декодирования (вся мозаика). */
    public int DecodeSize { get; set; } = 160;

    /** Скругление углов (по умолчанию 8). */
    public CornerRadius Radius
    {
        get => Frame.CornerRadius;
        set => Frame.CornerRadius = value;
    }

    private void Update()
    {
        var list = (Covers as IEnumerable<Uri>)?.ToList() ?? [];
        Single.Visibility = list.Count is > 0 and < 4 ? Visibility.Visible : Visibility.Collapsed;
        Quad.Visibility = list.Count >= 4 ? Visibility.Visible : Visibility.Collapsed;
        Placeholder.Visibility = list.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        Single.Source = list.Count is > 0 and < 4 ? Ui.Img(list[0], DecodeSize) : null;
        Image[] q = [Q0, Q1, Q2, Q3];
        for (var i = 0; i < 4; i++) q[i].Source = list.Count >= 4 ? Ui.Img(list[i], DecodeSize / 2) : null;
    }
}
