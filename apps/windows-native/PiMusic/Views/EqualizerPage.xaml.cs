using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Navigation;
using PiMusic.Player;
using Windows.Foundation;

namespace PiMusic.Views;

/**
 * Эквалайзер на 10 полос (EqController → eq.* в /api/settings): пресеты (встроенные и свои), полосы и преамп
 * вертикальными ползунками, кривая АЧХ, точная настройка полосы во всплывающем окне, режим sync/local, лимитер.
 * Перетаскивание ползунка: кривая и подпись — сразу, запись в настройки — через 150 мс после последнего сдвига.
 */
public sealed partial class EqualizerPage : Page
{
    private readonly EqController _eq = EqController.Instance;
    private readonly Slider[] _bands = new Slider[Eq.Freqs.Length];
    private readonly TextBlock[] _gainText = new TextBlock[Eq.Freqs.Length];
    private readonly Button[] _freqButtons = new Button[Eq.Freqs.Length];
    private Slider _preamp = null!;
    private TextBlock _preampText = null!;
    private List<Preset> _presets = [];
    private EqState _state = EqState.Flat;
    private bool _syncing;
    private readonly DispatcherQueueTimer _commit;
    private Action? _pending;

    private static readonly double[] CurveFreqs = [.. Enumerable.Range(0, 96).Select(i => 20 * Math.Pow(1000, i / 95.0))];

    public EqualizerPage()
    {
        InitializeComponent();
        BuildBands();
        _commit = DispatcherQueue.CreateTimer();
        _commit.Interval = TimeSpan.FromMilliseconds(150);
        _commit.IsRepeating = false;
        _commit.Tick += (_, _) => Commit();
    }

    protected override void OnNavigatedTo(NavigationEventArgs e)
    {
        _eq.Changed += Show;
        Show();
    }

    protected override void OnNavigatedFrom(NavigationEventArgs e)
    {
        Commit();
        _eq.Changed -= Show;
    }

    // ---------------------------------------------------------------- полосы

    private void BuildBands()
    {
        var col = 0;
        _preamp = AddColumn(col++, "Преамп", Eq.PreMin, Eq.PreMax, out _preampText, out _);
        _preamp.ValueChanged += (_, a) =>
        {
            if (_syncing) return;
            var v = Math.Round(a.NewValue, 1);
            _preampText.Text = EqFormat.Gain(v);
            _state = _state with { Preamp = v };
            DrawCurve();
            Later(() => _eq.SetPreamp(v));
        };
        // зазор между преампом и полосами
        BandsGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(12) });
        col++;
        for (var i = 0; i < _bands.Length; i++)
        {
            var k = i;
            _bands[i] = AddColumn(col++, EqFormat.FreqShort(Eq.Freqs[i]), Eq.GMin, Eq.GMax, out _gainText[i], out _freqButtons[i]);
            _bands[i].ValueChanged += (_, a) =>
            {
                if (_syncing) return;
                var g = Math.Round(a.NewValue, 1);
                _gainText[k].Text = EqFormat.Gain(g);
                var bands = _state.Bands.ToList();
                bands[k] = bands[k] with { G = g };
                _state = _state with { Bands = bands };
                DrawCurve();
                Later(() => _eq.SetBand(k, bands[k]));
            };
            _freqButtons[i].Click += (_, _) => EditBand(k);
        }
    }

    private Slider AddColumn(int col, string label, double min, double max, out TextBlock value, out Button freq)
    {
        BandsGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star), MinWidth = 44 });
        value = new TextBlock
        {
            Style = (Style)Application.Current.Resources["CaptionTextBlockStyle"],
            HorizontalAlignment = HorizontalAlignment.Center,
        };
        var slider = new Slider
        {
            Orientation = Orientation.Vertical,
            Minimum = min,
            Maximum = max,
            StepFrequency = 0.1,
            SmallChange = 0.5,
            LargeChange = 3,
            Height = 180,
            HorizontalAlignment = HorizontalAlignment.Center,
            IsThumbToolTipEnabled = false,
        };
        AutomationProperties.SetName(slider, label == "Преамп" ? "Преамп, дБ" : $"Полоса {label} Гц, дБ");
        freq = new Button
        {
            Content = label,
            Style = (Style)Application.Current.Resources["SubtleButtonStyle"],
            Padding = new Thickness(6, 2, 6, 2),
            HorizontalAlignment = HorizontalAlignment.Center,
            FontSize = 12,
        };
        if (label == "Преамп") freq.IsHitTestVisible = false;
        var panel = new StackPanel { Spacing = 6, Children = { value, slider, freq } };
        Grid.SetColumn(panel, col);
        BandsGrid.Children.Add(panel);
        return slider;
    }

    /** Отложенная запись в настройки: последний сдвиг ползунка выигрывает. */
    private void Later(Action write)
    {
        _pending = write;
        _commit.Stop();
        _commit.Start();
    }

    private void Commit()
    {
        _commit.Stop();
        var w = _pending;
        _pending = null;
        w?.Invoke();
    }

    // ---------------------------------------------------------------- показ

    private void Show()
    {
        if (_pending is not null) return; // идёт перетаскивание — ползунки не дёргать
        var (active, st) = _eq.Effective();
        var d = _eq.Device();
        _state = st;
        _presets = _eq.AllPresets();
        _syncing = true;
        try
        {
            EnabledSwitch.IsOn = st.Enabled;
            LimiterSwitch.IsOn = st.Limiter;
            SyncSwitch.IsOn = d.Mode != "local";
            _preamp.Value = st.Preamp;
            _preampText.Text = EqFormat.Gain(st.Preamp);
            for (var i = 0; i < _bands.Length; i++)
            {
                var b = st.Bands[i];
                _bands[i].Value = b.G;
                _gainText[i].Text = EqFormat.Gain(b.G);
                _freqButtons[i].Content = EqFormat.FreqShort(b.F);
                ToolTipService.SetToolTip(_freqButtons[i], $"{EqFormat.Freq(b.F)} · {EqFormat.Q(b.Q)} · {TypeName(b.Type)}");
            }
            var items = _presets.Select(p => p.Name).ToList();
            var idx = _presets.FindIndex(p => p.Id == active);
            if (idx < 0) { items.Add("Свой"); idx = items.Count - 1; }
            PresetBox.ItemsSource = items;
            PresetBox.SelectedIndex = idx;
            RenameItem.IsEnabled = DeleteItem.IsEnabled = _presets.FirstOrDefault(p => p.Id == active) is { Builtin: false };
        }
        finally { _syncing = false; }
        DeviceText.Text = $"Это устройство: {d.Name}. Звук с эквалайзером идёт через AudioGraph; поток, который он не открыл, " +
                          "играет без эквалайзера.";
        DrawCurve();
    }

    private static string TypeName(string type) => type switch
    {
        "lowshelf" => "НЧ-полка",
        "highshelf" => "ВЧ-полка",
        _ => "колокол",
    };

    private void Curve_SizeChanged(object sender, SizeChangedEventArgs e) => DrawCurve();

    private void DrawCurve()
    {
        var w = Curve.ActualWidth;
        var h = Curve.ActualHeight;
        if (w <= 0 || h <= 0) return;
        ZeroLine.X1 = 0;
        ZeroLine.X2 = w;
        ZeroLine.Y1 = ZeroLine.Y2 = h / 2;
        var db = Eq.ResponseDb(_state, CurveFreqs);
        var pts = new PointCollection();
        for (var i = 0; i < CurveFreqs.Length; i++)
            pts.Add(new Point(i / (double)(CurveFreqs.Length - 1) * w, h / 2 - Math.Clamp(db[i], -12, 12) / 12 * (h / 2 - 2)));
        CurveLine.Points = pts;
        CurveLine.Opacity = _state.Enabled ? 1 : 0.4;
    }

    // ---------------------------------------------------------------- действия

    private void Enabled_Toggled(object sender, RoutedEventArgs e)
    {
        if (!_syncing) _eq.SetEnabled(EnabledSwitch.IsOn);
    }

    private void Limiter_Toggled(object sender, RoutedEventArgs e)
    {
        if (!_syncing) _eq.SetLimiter(LimiterSwitch.IsOn);
    }

    private void Sync_Toggled(object sender, RoutedEventArgs e)
    {
        if (!_syncing) _eq.SetMode(SyncSwitch.IsOn ? "sync" : "local");
    }

    private void Preset_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_syncing || PresetBox.SelectedIndex < 0 || PresetBox.SelectedIndex >= _presets.Count) return;
        _pending = null;
        _eq.ApplyPreset(_presets[PresetBox.SelectedIndex].Id);
    }

    private void Reset_Click(object sender, RoutedEventArgs e)
    {
        _pending = null;
        _eq.ApplyPreset("flat");
    }

    private async void SavePreset_Click(object sender, RoutedEventArgs e)
    {
        Commit();
        var name = await AskName("Сохранить пресет", "Мой пресет", "Сохранить");
        if (name is null) return;
        _eq.SavePreset(name);
        Ui.Success($"Пресет «{name}» сохранён");
    }

    private async void RenamePreset_Click(object sender, RoutedEventArgs e)
    {
        var (active, _) = _eq.Effective();
        if (_presets.FirstOrDefault(p => p.Id == active) is not { Builtin: false } p) return;
        var name = await AskName("Переименовать пресет", p.Name, "Сохранить");
        if (name is not null) _eq.RenamePreset(p.Id, name);
    }

    private async void DeletePreset_Click(object sender, RoutedEventArgs e)
    {
        var (active, _) = _eq.Effective();
        if (_presets.FirstOrDefault(p => p.Id == active) is not { Builtin: false } p) return;
        if (!await Ui.Confirm(XamlRoot, "Удалить пресет?", $"«{p.Name}» исчезнет на всех ваших устройствах. Текущий звук не изменится.", "Удалить")) return;
        _eq.DeletePreset(p.Id);
    }

    private async Task<string?> AskName(string title, string initial, string action)
    {
        var box = new TextBox { Text = initial, MaxLength = 40, MinWidth = 320, Header = "Название" };
        var d = new ContentDialog
        {
            XamlRoot = XamlRoot,
            Title = title,
            Content = box,
            PrimaryButtonText = action,
            CloseButtonText = "Отмена",
            DefaultButton = ContentDialogButton.Primary,
            Style = (Style)Application.Current.Resources["DefaultContentDialogStyle"],
        };
        box.TextChanged += (_, _) => d.IsPrimaryButtonEnabled = box.Text.Trim().Length > 0;
        box.Loaded += (_, _) => box.SelectAll();
        return await d.ShowAsync() == ContentDialogResult.Primary ? box.Text.Trim() : null;
    }

    /** Точная настройка полосы: частота, усиление, ширина (Q), тип (полки — только у крайних полос). */
    private void EditBand(int i)
    {
        Commit();
        var b = _state.Bands[i];
        var edge = i == 0 || i == _bands.Length - 1;
        static NumberBox Num(string header, double value, double min, double max, double step) => new()
        {
            Header = header, Value = value, Minimum = min, Maximum = max, SmallChange = step, LargeChange = step * 10,
            SpinButtonPlacementMode = NumberBoxSpinButtonPlacementMode.Inline, MinWidth = 220,
            ValidationMode = NumberBoxValidationMode.InvalidInputOverwritten,
        };
        var f = Num("Частота, Гц", b.F, Eq.FMin, Eq.FMax, 1);
        var g = Num("Усиление, дБ", b.G, Eq.GMin, Eq.GMax, 0.1);
        var q = Num("Ширина (Q)", b.Q, Eq.QMin, Eq.QMax, 0.05);
        var type = new ComboBox { Header = "Тип", MinWidth = 220, ItemsSource = new[] { "Колокол", "НЧ-полка", "ВЧ-полка" } };
        type.SelectedIndex = b.Type switch { "lowshelf" => 1, "highshelf" => 2, _ => 0 };
        type.IsEnabled = edge;
        var apply = new Button { Content = "Применить", Style = (Style)Application.Current.Resources["AccentButtonStyle"], HorizontalAlignment = HorizontalAlignment.Right };
        var panel = new StackPanel { Spacing = 12, Children = { f, g, q, type } };
        if (!edge)
            panel.Children.Add(new TextBlock
            {
                Text = "Полки — только у крайних полос",
                Style = (Style)Application.Current.Resources["CaptionTextBlockStyle"],
                Foreground = (Brush)Application.Current.Resources["TextFillColorSecondaryBrush"],
            });
        panel.Children.Add(apply);
        var fly = new Flyout { Content = panel, Placement = FlyoutPlacementMode.Bottom };
        apply.Click += (_, _) =>
        {
            var t = type.SelectedIndex switch { 1 => "lowshelf", 2 => "highshelf", _ => "peaking" };
            static double V(NumberBox n, double fallback) => double.IsNaN(n.Value) ? fallback : n.Value;
            _eq.SetBand(i, new Band(t, V(f, b.F), V(g, b.G), V(q, b.Q)));
            fly.Hide();
        };
        fly.ShowAt(_freqButtons[i]);
    }
}
