using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using PiMusic.Data;

namespace PiMusic.Views;

/** Помощники для x:Bind-функций и общих действий интерфейса. */
public static class Ui
{
    private static readonly Dictionary<(Uri, int), WeakReference<BitmapImage>> Images = [];

    /** Картинка по адресу с декодированием под нужный размер (px), с кэшем; null — без картинки (видна заглушка). */
    public static ImageSource? Img(Uri? uri, int size)
    {
        if (uri is null) return null;
        if (Images.TryGetValue((uri, size), out var w) && w.TryGetTarget(out var cached)) return cached;
        if (Images.Count > 500)
            foreach (var k in Images.Where(p => !p.Value.TryGetTarget(out _)).Select(p => p.Key).ToList()) Images.Remove(k);
        var bmp = new BitmapImage { DecodePixelWidth = size, DecodePixelType = DecodePixelType.Logical, UriSource = uri };
        Images[(uri, size)] = new WeakReference<BitmapImage>(bmp);
        return bmp;
    }

    public static ImageSource? Cover40(Uri? uri) => Img(uri, 40);
    public static ImageSource? Cover160(Uri? uri) => Img(uri, 160);
    public static ImageSource? Cover240(Uri? uri) => Img(uri, 240);
    public static ImageSource? Cover48(Uri? uri) => Img(uri, 48);
    /** Для x:Bind по текущему треку плеера (Current может быть null). */
    public static ImageSource? TrackCover56(Track? t) => Img(t?.CoverUri, 56);
    public static ImageSource? TrackCover420(Track? t) => Img(t?.CoverUri, 420);
    public static string ArtistOf(Track? t) => t?.Artist ?? "";
    public static string AlbumOf(Track? t) => t?.Album ?? "";
    public static bool HasTrack(Track? t) => t is not null;

    public static Visibility Show(bool v) => v ? Visibility.Visible : Visibility.Collapsed;
    public static Visibility Hide(bool v) => v ? Visibility.Collapsed : Visibility.Visible;
    public static Visibility ShowText(string? s) => string.IsNullOrEmpty(s) ? Visibility.Collapsed : Visibility.Visible;
    public static Visibility ShowObj(object? o) => o is null ? Visibility.Collapsed : Visibility.Visible;
    public static string Year(int? y) => y is > 0 ? y.Value.ToString() : "";
    public static string Join(params string?[] parts) => Fmt.Join(parts);

    /** Сообщение об ошибке: InfoBar в окне (MainWindow.Toast). */
    public static void Error(Exception e) => App.Window?.Toast(e is ApiException ? e.Message : "Что-то пошло не так: " + e.Message, InfoBarSeverity.Error);
    public static void Info(string text) => App.Window?.Toast(text, InfoBarSeverity.Informational);
    public static void Success(string text) => App.Window?.Toast(text, InfoBarSeverity.Success);
    /** Сообщение об успехе с кнопкой действия. */
    public static void Success(string text, string actionText, Action action) => App.Window?.Toast(text, InfoBarSeverity.Success, actionText, action);

    /** Диалог подтверждения в стиле Fluent; true — нажата основная кнопка. */
    public static async Task<bool> Confirm(XamlRoot root, string title, string text, string primary)
    {
        var d = new ContentDialog
        {
            XamlRoot = root,
            Title = title,
            Content = text,
            PrimaryButtonText = primary,
            CloseButtonText = "Отмена",
            DefaultButton = ContentDialogButton.Close,
            Style = (Style)Application.Current.Resources["DefaultContentDialogStyle"],
        };
        return await d.ShowAsync() == ContentDialogResult.Primary;
    }
}
