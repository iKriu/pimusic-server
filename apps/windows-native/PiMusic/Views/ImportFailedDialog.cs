using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using PiMusic.Data;

namespace PiMusic.Views;

/** Диалог «Не найдено»: треки списка, которые импорт не смог найти. */
public static class ImportFailedDialog
{
    public static async Task Show(XamlRoot root, long importId, string filename)
    {
        ImportFailed r;
        try { r = await Api.Instance.ImportFailed(importId); }
        catch (Exception ex) { Ui.Error(ex); return; }

        var lines = r.Items.Select(i =>
        {
            var name = string.IsNullOrEmpty(i.Artist) ? i.Title : $"{i.Artist} — {i.Title}";
            return Fmt.Join(name, i.Album, i.Note);
        }).ToList();

        UIElement content;
        if (lines.Count == 0)
            content = new TextBlock { Text = "Ненайденных треков нет.", TextWrapping = TextWrapping.Wrap };
        else
        {
            var list = new StackPanel { Spacing = 6 };
            foreach (var l in lines)
                list.Children.Add(new TextBlock { Text = l, TextWrapping = TextWrapping.Wrap, IsTextSelectionEnabled = true });
            content = new ScrollViewer { Content = list, MaxHeight = 420, Padding = new Thickness(0, 0, 12, 0) };
        }

        var d = new ContentDialog
        {
            XamlRoot = root,
            Title = string.IsNullOrEmpty(filename) ? $"Не найдено: {Fmt.Tracks(lines.Count)}" : $"Не найдено в «{filename}»: {Fmt.Tracks(lines.Count)}",
            Content = content,
            CloseButtonText = "Закрыть",
            DefaultButton = ContentDialogButton.Close,
            Style = (Style)Application.Current.Resources["DefaultContentDialogStyle"],
        };
        if (lines.Count > 0)
        {
            d.PrimaryButtonText = "Копировать список";
            d.PrimaryButtonClick += (_, _) =>
            {
                var p = new Windows.ApplicationModel.DataTransfer.DataPackage();
                p.SetText(string.Join(Environment.NewLine, lines));
                Windows.ApplicationModel.DataTransfer.Clipboard.SetContent(p);
                Ui.Success("Список скопирован");
            };
        }
        await d.ShowAsync();
    }
}
