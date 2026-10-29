using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Navigation;
using PiMusic.Data;
using PiMusic.Player;

namespace PiMusic.Views;

/** Интернет-радио (/api/radio): щелчок — слушать поток станции; добавить — POST /api/radio; удалить — администратор. */
public sealed partial class RadioPage : Page
{
    private bool _admin;

    public RadioPage() => InitializeComponent();

    protected override void OnNavigatedTo(NavigationEventArgs e) => _ = Load();

    private async Task Load()
    {
        Busy.IsActive = true;
        try
        {
            var r = await Api.Instance.Radio();
            List.ItemsSource = r.Items;
            CountText.Text = r.Items.Count == 0 ? "" : $"Станций: {r.Items.Count}";
            Empty.Visibility = Ui.Show(r.Items.Count == 0);
            try { _admin = (await Api.Instance.Me()).IsAdmin; } catch (Exception) { }
        }
        catch (Exception ex) { Ui.Error(ex); }
        finally { Busy.IsActive = false; }
    }

    private void List_ItemClick(object sender, ItemClickEventArgs e)
    {
        if (e.ClickedItem is Station s) PlayerService.Instance.PlayRadio(s);
    }

    private void List_ContextRequested(UIElement sender, ContextRequestedEventArgs e)
    {
        if ((e.OriginalSource as FrameworkElement)?.DataContext is not Station s) return;
        var m = new MenuFlyout();
        var play = new MenuFlyoutItem { Text = "Слушать", Icon = new FontIcon { Glyph = "" } };
        play.Click += (_, _) => PlayerService.Instance.PlayRadio(s);
        m.Items.Add(play);
        var copy = new MenuFlyoutItem { Text = "Копировать адрес потока", Icon = new FontIcon { Glyph = "" } };
        copy.Click += (_, _) =>
        {
            var dp = new Windows.ApplicationModel.DataTransfer.DataPackage();
            dp.SetText(s.Url);
            Windows.ApplicationModel.DataTransfer.Clipboard.SetContent(dp);
            Ui.Info("Адрес скопирован");
        };
        m.Items.Add(copy);
        if (_admin)
        {
            m.Items.Add(new MenuFlyoutSeparator());
            var del = new MenuFlyoutItem { Text = "Удалить станцию", Icon = new FontIcon { Glyph = "" } };
            del.Click += async (_, _) =>
            {
                if (!await Ui.Confirm(XamlRoot, "Удалить станцию?", $"«{s.Name}» исчезнет у всех пользователей.", "Удалить")) return;
                try
                {
                    await Api.Instance.DeleteRadio(s.Id);
                    Ui.Success("Станция удалена");
                    await Load();
                }
                catch (Exception ex) { Ui.Error(ex); }
            };
            m.Items.Add(del);
        }
        var target = e.OriginalSource as FrameworkElement;
        if (e.TryGetPosition(target, out var p)) m.ShowAt(target, new FlyoutShowOptions { Position = p });
        else m.ShowAt(target);
        e.Handled = true;
    }

    /** «Добавить станцию»: имя и адрес потока (http/https). */
    private async void Add_Click(object sender, RoutedEventArgs e)
    {
        var name = new TextBox { Header = "Название", PlaceholderText = "Например, Радио Джаз", MaxLength = 100 };
        var url = new TextBox { Header = "Адрес потока", PlaceholderText = "https://…" };
        var d = new ContentDialog
        {
            XamlRoot = XamlRoot,
            Title = "Новая станция",
            Content = new StackPanel { Spacing = 12, MinWidth = 360, Children = { name, url } },
            PrimaryButtonText = "Добавить",
            CloseButtonText = "Отмена",
            DefaultButton = ContentDialogButton.Primary,
            IsPrimaryButtonEnabled = false,
            Style = (Style)Application.Current.Resources["DefaultContentDialogStyle"],
        };
        void Check(object sender, TextChangedEventArgs args) => d.IsPrimaryButtonEnabled = name.Text.Trim().Length > 0
            && Uri.TryCreate(url.Text.Trim(), UriKind.Absolute, out var u) && u.Scheme is "http" or "https";
        name.TextChanged += Check;
        url.TextChanged += Check;
        if (await d.ShowAsync() != ContentDialogResult.Primary) return;
        try
        {
            var s = await Api.Instance.AddRadio(name.Text.Trim(), url.Text.Trim());
            Ui.Success($"Станция «{s.Name}» добавлена");
            await Load();
        }
        catch (Exception ex) { Ui.Error(ex); }
    }
}
