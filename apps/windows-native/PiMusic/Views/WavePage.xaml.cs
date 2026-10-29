using System.ComponentModel;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Navigation;
using PiMusic.Data;
using PiMusic.Player;

namespace PiMusic.Views;

/**
 * «Моя Волна»: бесконечный MP3-поток stream_url из /api/me; что в эфире — /api/wave/now (PlayerService опрашивает,
 * пока играет; здесь — один раз при открытии); «Обновить плейлист» — /api/wave/refresh; «Найти новое» — /api/wave/discover.
 */
public sealed partial class WavePage : Page
{
    private readonly PlayerService _p = PlayerService.Instance;
    private WaveNow? _now;
    private bool _busy;

    public WavePage() => InitializeComponent();

    protected override void OnNavigatedTo(NavigationEventArgs e)
    {
        _p.PropertyChanged += OnPlayer;
        Show();
        _ = LoadCovers();
        _ = LoadNow();
    }

    protected override void OnNavigatedFrom(NavigationEventArgs e) => _p.PropertyChanged -= OnPlayer;

    private void OnPlayer(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName is nameof(PlayerService.LiveKind) or nameof(PlayerService.IsPlaying) or nameof(PlayerService.Wave)
            or nameof(PlayerService.Current))
            Show();
    }

    private bool OnAir => _p.LiveKind == "wave";

    /** Обложки последних альбомов — мозаика, пока не известно, что в эфире. */
    private async Task LoadCovers()
    {
        try
        {
            var a = await Api.Instance.Albums(sort: "added");
            var covers = a.Items.Select(x => x.CoverUri).OfType<Uri>().Take(4).ToList();
            if (covers.Count == 0) return;
            Image[] cells = [M0, M1, M2, M3];
            if (covers.Count < 4) { NowCover.Source ??= Ui.Img(covers[0], 240); return; }
            for (var i = 0; i < cells.Length; i++) cells[i].Source = Ui.Img(covers[i], 120);
        }
        catch (Exception) { /* без мозаики — значок */ }
    }

    private async Task LoadNow()
    {
        try
        {
            _now = await Api.Instance.WaveNow();
            Show();
        }
        catch (Exception) { }
    }

    private void Show()
    {
        var now = OnAir ? _p.Wave ?? _now : _now;
        var playing = OnAir && _p.IsPlaying;
        if (OnAir && _p.Current is { Live: true } cur && now is { Title.Length: > 0 })
        {
            Kicker.Text = "Сейчас в эфире";
            TitleText.Text = cur.Title;
            ArtistText.Text = cur.Artist;
            if (cur.CoverUri is not null) NowCover.Source = Ui.Img(cur.CoverUri, 240);
        }
        else if (now is { Live: true, Title: { Length: > 0 } t })
        {
            Kicker.Text = "В эфире";
            TitleText.Text = t;
            ArtistText.Text = now.Artist ?? "";
            if (now.CoverUri is not null) NowCover.Source = Ui.Img(now.CoverUri, 240);
        }
        else
        {
            Kicker.Text = "Бесконечный эфир из вашей медиатеки";
            TitleText.Text = "Слушайте свою музыку без конца";
            ArtistText.Text = "";
        }
        ArtistText.Visibility = Ui.ShowText(ArtistText.Text);
        ListenersText.Text = now is { Listeners: > 0 } ? $"Слушают: {now.Listeners}" : "";
        ListenersText.Visibility = Ui.ShowText(ListenersText.Text);
        PlayIcon.Glyph = playing ? "" : "";
        PlayText.Text = playing ? "Пауза" : OnAir ? "Продолжить" : "Слушать";
        AutomationProperties.SetName(PlayButton, playing ? "Пауза" : "Слушать «Мою Волну»");
    }

    private async void Play_Click(object sender, RoutedEventArgs e)
    {
        if (OnAir) { _p.TogglePlay(); return; }
        PlayButton.IsEnabled = false;
        try
        {
            var me = await Api.Instance.Me();
            if (Api.Instance.Abs(me.StreamUrl) is { } u) _p.PlayWave(u);
            else Ui.Info("Эфир пока недоступен — попробуйте через минуту");
        }
        catch (Exception ex) { Ui.Error(ex); }
        finally { PlayButton.IsEnabled = true; }
    }

    private async void Refresh_Click(object sender, RoutedEventArgs e) => await Run(async () =>
    {
        var r = await Api.Instance.WaveRefresh();
        Ui.Success($"Плейлист Волны обновлён: {Fmt.Tracks(r.Tracks)}");
    });

    private async void Discover_Click(object sender, RoutedEventArgs e) => await Run(async () =>
    {
        var r = await Api.Instance.WaveDiscover();
        FoundList.ItemsSource = r.Queued;
        FoundPanel.Visibility = Ui.Show(r.Queued.Count > 0);
        if (r.Queued.Count == 0) Ui.Info("Новых треков не нашлось");
        else Ui.Success($"Качаю новое: {Fmt.Tracks(r.Queued.Count)}");
    });

    private async Task Run(Func<Task> action)
    {
        if (_busy) return;
        _busy = true;
        RefreshButton.IsEnabled = DiscoverButton.IsEnabled = false;
        Busy.Visibility = Visibility.Visible;
        try { await action(); }
        catch (Exception ex) { Ui.Error(ex); }
        finally
        {
            _busy = false;
            RefreshButton.IsEnabled = DiscoverButton.IsEnabled = true;
            Busy.Visibility = Visibility.Collapsed;
        }
    }
}
