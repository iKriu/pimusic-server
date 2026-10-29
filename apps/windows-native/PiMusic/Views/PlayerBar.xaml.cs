using System.ComponentModel;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Data;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using PiMusic.Data;
using PiMusic.Player;

namespace PiMusic.Views;

public sealed partial class PlayerBar : UserControl, INotifyPropertyChanged
{
    public PlayerService P { get; } = PlayerService.Instance;
    public IValueConverter TimeConverter { get; } = new SecondsConverter();

    private bool _dragging;
    private bool _fromPlayer;

    public event PropertyChangedEventHandler? PropertyChanged;

    public PlayerBar()
    {
        InitializeComponent();
        P.PropertyChanged += Player_PropertyChanged;
        Seek.AddHandler(PointerPressedEvent, new PointerEventHandler((_, _) => _dragging = true), true);
        Seek.AddHandler(PointerReleasedEvent, new PointerEventHandler((_, _) => EndDrag()), true);
        Seek.AddHandler(PointerCaptureLostEvent, new PointerEventHandler((_, _) => EndDrag()), true);
        UpdateProgress();
    }

    private void Player_PropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        switch (e.PropertyName)
        {
            case nameof(PlayerService.Position):
            case nameof(PlayerService.Duration):
            case nameof(PlayerService.LiveKind):
                UpdateProgress();
                break;
            case nameof(PlayerService.Volume):
            case nameof(PlayerService.IsMuted):
                PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(VolumePercent)));
                break;
        }
    }

    private void UpdateProgress()
    {
        // прямой эфир: без перемотки, вместо длительности — «Эфир»
        Seek.IsEnabled = !P.IsLive;
        DurationText.Text = P.IsLive ? "Эфир" : P.Duration > 0 ? Fmt.Time(TimeSpan.FromSeconds(P.Duration)) : P.Current?.Duration ?? "0:00";
        if (_dragging) return;
        _fromPlayer = true;
        Seek.Maximum = Math.Max(1, P.Duration);
        Seek.Value = Math.Min(P.Position, Seek.Maximum);
        _fromPlayer = false;
        PositionText.Text = Fmt.Time(TimeSpan.FromSeconds(P.Position));
    }

    private void EndDrag()
    {
        if (!_dragging) return;
        _dragging = false;
        P.Seek(Seek.Value);
    }

    private void Seek_ValueChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (_fromPlayer) return;
        PositionText.Text = Fmt.Time(TimeSpan.FromSeconds(e.NewValue));
        if (!_dragging) P.Seek(e.NewValue); // клавиатура и щелчок по дорожке
    }

    public double VolumePercent
    {
        get => P.IsMuted ? 0 : Math.Round(P.Volume * 100);
        set
        {
            if (Math.Abs(value - VolumePercent) < 0.5) return;
            P.Volume = value / 100;
            if (value > 0 && P.IsMuted) P.IsMuted = false;
        }
    }

    // ---------------------------------------------------------------- подписи и значки (x:Bind)

    public static string TitleOf(Track? t) => t?.Title ?? "Ничего не играет";
    public string PlayGlyph(bool playing) => playing ? "" : "";
    public string PlayLabel(bool playing) => playing ? "Пауза" : "Воспроизвести";
    public string MuteLabel(bool muted) => muted ? "Включить звук" : "Выключить звук";

    public string VolumeGlyph(double volume, bool muted) =>
        muted || volume <= 0 ? "" : volume < 0.34 ? "" : volume < 0.67 ? "" : "";

    public string RepeatGlyph(RepeatMode r) => r == RepeatMode.One ? "" : "";

    public string RepeatLabel(RepeatMode r) => r switch
    {
        RepeatMode.All => "Повтор очереди",
        RepeatMode.One => "Повтор трека",
        _ => "Повтор выключен",
    };

    // Два значка повтора (обычный и акцентный) с кистями ThemeResource — так они следуют теме окна.
    public Visibility RepeatOffShow(RepeatMode r) => r == RepeatMode.Off ? Visibility.Visible : Visibility.Collapsed;
    public Visibility RepeatOnShow(RepeatMode r) => r == RepeatMode.Off ? Visibility.Collapsed : Visibility.Visible;

    // ---------------------------------------------------------------- кнопки

    private void Play_Click(object sender, RoutedEventArgs e) => P.TogglePlay();
    private void Prev_Click(object sender, RoutedEventArgs e) => P.Previous();
    private void Next_Click(object sender, RoutedEventArgs e) => P.Next();
    private void Mute_Click(object sender, RoutedEventArgs e) => P.IsMuted = !P.IsMuted;

    private void Repeat_Click(object sender, RoutedEventArgs e) =>
        P.Repeat = P.Repeat switch { RepeatMode.Off => RepeatMode.All, RepeatMode.All => RepeatMode.One, _ => RepeatMode.Off };

    private void TrackButton_Click(object sender, RoutedEventArgs e) => App.Window?.Navigate(typeof(NowPlayingPage));
    private void Eq_Click(object sender, RoutedEventArgs e) => App.Window?.Navigate(typeof(EqualizerPage));

    private sealed class SecondsConverter : IValueConverter
    {
        public object Convert(object value, Type targetType, object parameter, string language) =>
            Fmt.Time(TimeSpan.FromSeconds(value is double d ? d : 0));

        public object ConvertBack(object value, Type targetType, object parameter, string language) => throw new NotSupportedException();
    }
}
