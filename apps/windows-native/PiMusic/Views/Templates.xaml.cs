using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using PiMusic.Data;

namespace PiMusic.Views;

public sealed partial class Templates : ResourceDictionary
{
    public Templates() => InitializeComponent();

    /** «Скачать» у трека каталога: страница поиска подписывается через CatalogDownload. */
    private void DownloadTrack_Click(object sender, RoutedEventArgs e)
    {
        if (sender is Button { Tag: CTrack t } b)
        {
            b.IsEnabled = false;
            CatalogDownload?.Invoke(t);
        }
    }

    /** Обработчик «Скачать» трека каталога (страница, где показан список). */
    public static Action<CTrack>? CatalogDownload { get; set; }
}
