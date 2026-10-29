using System.Globalization;

namespace PiMusic.Player;

/** Полоса эквалайзера — формат eq.js: {type: peaking|lowshelf|highshelf, f: 20…20000, g: −12…12, q: 0.3…8}. */
public sealed record Band(string Type, double F, double G, double Q);

/** Профиль звука: полосы + преамп; Enabled/Limiter — свойства устройства. */
public sealed record EqState(bool Enabled, double Preamp, IReadOnlyList<Band> Bands, bool Limiter)
{
    public static EqState Flat { get; } = new(true, 0, Eq.FlatBands(), true);

    /** Звук меняется — нужна обработка (иначе играем без неё). */
    public bool Active => Enabled && (Math.Abs(Preamp) >= 0.05 || Bands.Any(b => Math.Abs(b.G) >= 0.05));
}

public sealed record Preset(string Id, string Name, double Preamp, IReadOnlyList<Band> Bands, bool Builtin = false);

/** Константы и пресеты эквалайзера — те же, что в eq.js и Android (core/Eq.kt). */
public static class Eq
{
    public static readonly double[] Freqs = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];
    public const double FMin = 20, FMax = 20000, GMin = -12, GMax = 12, QMin = 0.3, QMax = 8, PreMin = -12, PreMax = 6;

    public static List<Band> FlatBands() => [.. Freqs.Select(f => new Band("peaking", f, 0, 1))];

    private static Preset P(string id, string name, double[] g)
    {
        var max = Math.Max(0, g.Max());
        return new Preset(id, name, -Math.Round(max * 0.5 * 2, MidpointRounding.AwayFromZero) / 2,
            [.. Freqs.Select((f, i) => new Band("peaking", f, g[i], 1))], true);
    }

    public static readonly IReadOnlyList<Preset> Builtin =
    [
        P("flat", "Ровно", [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
        P("bass", "Бас+", [6, 5, 4, 2, 0.5, 0, 0, 0, 0, 0]),
        P("treble", "Высокие+", [0, 0, 0, 0, 0, 0.5, 2, 4, 5, 6]),
        P("vocal", "Вокал", [-2, -2, -1, 0, 2, 3, 3, 2, 0, -1]),
        P("rock", "Рок", [4, 3, 2, 0, -1, -1, 1, 2.5, 3.5, 4]),
        P("electro", "Электроника", [5, 4, 2, 0, -2, 0, 1, 2, 4, 5]),
        P("classic", "Классика", [3, 2, 1, 0, 0, 0, -1, -1, 1, 2]),
        P("podcast", "Подкаст", [-6, -4, -2, 0, 2, 3, 3, 2, 0, -2]),
        P("warm", "Наушники · тёплые", [3, 3, 2, 1, 0, 0, -1, -2, -2, -3]),
        P("night", "Громкость ночью", [-4, -3, -1, 0, 1, 2, 2, 1, 0, -2]),
    ];

    /** Как sanitizeBands в eq.js: ровно 10 полос, полки только у крайних, значения в границах, усиление с шагом 0,1. */
    public static List<Band> Sanitize(IReadOnlyList<Band>? bands)
    {
        if (bands is null || bands.Count != Freqs.Length) return FlatBands();
        return [.. bands.Select((b, i) =>
        {
            var edge = i == 0 || i == Freqs.Length - 1;
            var type = b.Type is "peaking" or "lowshelf" or "highshelf" && (edge || b.Type == "peaking") ? b.Type : "peaking";
            var f = double.IsFinite(b.F) && b.F > 0 ? Math.Clamp(b.F, FMin, FMax) : Freqs[i];
            var g = Math.Clamp(Math.Round((double.IsFinite(b.G) ? b.G : 0) * 10, MidpointRounding.AwayFromZero) / 10, GMin, GMax);
            var q = double.IsFinite(b.Q) && b.Q > 0 ? Math.Clamp(b.Q, QMin, QMax) : 1;
            return new Band(type, f, g, q);
        })];
    }

    public static double DbToGain(double db) => Math.Pow(10, db / 20);

    /** АЧХ всего эквалайзера в дБ на частотах freqs (кривая на экране). */
    public static double[] ResponseDb(EqState s, double[] freqs, double sampleRate = 48000)
    {
        var coefs = s.Bands.Select(b => Biquad.Of(b, sampleRate)).ToArray();
        return [.. freqs.Select(f => s.Preamp + coefs.Sum(c => c.MagnitudeDb(f, sampleRate)))];
    }
}

/** Коэффициенты биквада (RBJ Audio EQ Cookbook — как BiquadFilterNode в Web Audio), нормированы на a0. */
public readonly record struct Biquad(double B0, double B1, double B2, double A1, double A2)
{
    public static Biquad Of(Band band, double sampleRate)
    {
        var f = Math.Min(band.F, sampleRate / 2 * 0.999);
        var w0 = 2 * Math.PI * f / sampleRate;
        var cw = Math.Cos(w0);
        var sw = Math.Sin(w0);
        var a = Math.Pow(10, band.G / 40);
        switch (band.Type)
        {
            case "lowshelf":
            {
                // Web Audio: наклон S = 1 (Q у полок не используется)
                var k = 2 * Math.Sqrt(a) * (sw / 2 * Math.Sqrt(2));
                var a0 = (a + 1) + (a - 1) * cw + k;
                return new Biquad(a * ((a + 1) - (a - 1) * cw + k) / a0, 2 * a * ((a - 1) - (a + 1) * cw) / a0,
                    a * ((a + 1) - (a - 1) * cw - k) / a0, -2 * ((a - 1) + (a + 1) * cw) / a0, ((a + 1) + (a - 1) * cw - k) / a0);
            }
            case "highshelf":
            {
                var k = 2 * Math.Sqrt(a) * (sw / 2 * Math.Sqrt(2));
                var a0 = (a + 1) - (a - 1) * cw + k;
                return new Biquad(a * ((a + 1) + (a - 1) * cw + k) / a0, -2 * a * ((a - 1) + (a + 1) * cw) / a0,
                    a * ((a + 1) + (a - 1) * cw - k) / a0, 2 * ((a - 1) - (a + 1) * cw) / a0, ((a + 1) - (a - 1) * cw - k) / a0);
            }
            default:
            {
                var alpha = sw / (2 * band.Q);
                var a0 = 1 + alpha / a;
                return new Biquad((1 + alpha * a) / a0, -2 * cw / a0, (1 - alpha * a) / a0, -2 * cw / a0, (1 - alpha / a) / a0);
            }
        }
    }

    /** |H(f)| в дБ. */
    public double MagnitudeDb(double freq, double sampleRate)
    {
        var w = 2 * Math.PI * freq / sampleRate;
        double c1 = Math.Cos(w), s1 = Math.Sin(w), c2 = Math.Cos(2 * w), s2 = Math.Sin(2 * w);
        var nr = B0 + B1 * c1 + B2 * c2;
        var ni = -(B1 * s1 + B2 * s2);
        var dr = 1 + A1 * c1 + A2 * c2;
        var di = -(A1 * s1 + A2 * s2);
        return 20 * Math.Log10(Math.Max(Math.Sqrt((nr * nr + ni * ni) / (dr * dr + di * di)), 1e-9));
    }
}

/**
 * Обработка звука в потоке AudioGraph: преамп × нормализация трека → биквады на канал → лимитер (−1,5 дБ, мгновенная
 * атака, спад ~80 мс). Параметры меняются из потока интерфейса заменой неизменяемого набора (без блокировок).
 */
public sealed class EqDsp
{
    private sealed class Params(Biquad[] coefs, double pre, bool limiter)
    {
        public readonly Biquad[] Coefs = coefs;
        public readonly double Pre = pre;
        public readonly bool Limiter = limiter;
    }

    private volatile Params _p = new([], 1, false);
    private EqState _state = EqState.Flat;
    private double _trackGain = 1;
    private int _rate = 48000;

    // состояние фильтров: [полоса][канал][z1, z2] — только в потоке звука
    private double[][][] _z = [];
    private double _env = 1;
    private static readonly double Thr = Eq.DbToGain(-1.5);

    public int SampleRate
    {
        get => _rate;
        set { if (_rate != value) { _rate = value; Rebuild(); } }
    }

    public void Set(EqState state)
    {
        _state = state;
        Rebuild();
    }

    /** Множитель нормализации текущего трека (ReplayGain); подъём громкости страхует лимитер. */
    public void SetTrackGain(double g)
    {
        if (Math.Abs(_trackGain - g) < 1e-6) return;
        _trackGain = g;
        Rebuild();
    }

    private void Rebuild()
    {
        var eqOn = _state.Enabled;
        var bands = eqOn ? _state.Bands.Where(b => Math.Abs(b.G) >= 0.05).Select(b => Biquad.Of(b, _rate)).ToArray() : [];
        var pre = (eqOn ? Eq.DbToGain(_state.Preamp) : 1) * _trackGain;
        _p = new Params(bands, pre, _state.Limiter || pre > 1 || bands.Any(b => b.B0 > 1));
    }

    /** Обработать чередующиеся float-отсчёты на месте (поток звука). */
    public unsafe void Process(float* data, int frames, int channels)
    {
        var p = _p;
        var coefs = p.Coefs;
        if (coefs.Length == 0 && Math.Abs(p.Pre - 1) < 1e-6 && !p.Limiter) return;
        if (_z.Length != coefs.Length || (_z.Length > 0 && _z[0].Length != channels))
            _z = [.. Enumerable.Range(0, coefs.Length).Select(_ => Enumerable.Range(0, channels).Select(_ => new double[2]).ToArray())];
        var release = 1 - Math.Exp(-1.0 / (0.08 * _rate));
        for (var n = 0; n < frames; n++)
        {
            var frame = data + n * channels;
            var peak = 0.0;
            for (var c = 0; c < channels; c++)
            {
                double x = frame[c] * p.Pre;
                for (var b = 0; b < coefs.Length; b++)
                {
                    var k = coefs[b];
                    var z = _z[b][c];
                    // прямая форма II, транспонированная
                    var y = k.B0 * x + z[0];
                    z[0] = k.B1 * x - k.A1 * y + z[1];
                    z[1] = k.B2 * x - k.A2 * y;
                    x = y;
                }
                frame[c] = (float)x;
                peak = Math.Max(peak, Math.Abs(x));
            }
            if (!p.Limiter) continue;
            var want = peak > Thr ? Thr / peak : 1.0;
            _env = want < _env ? want : _env + (want - _env) * release;
            if (_env < 0.99999)
                for (var c = 0; c < channels; c++) frame[c] = (float)Math.Clamp(frame[c] * _env, -1, 1);
        }
    }

    /** Новый источник: сбросить хвосты фильтров. */
    public void Reset()
    {
        foreach (var b in _z) foreach (var c in b) Array.Clear(c);
        _env = 1;
    }
}

/** Ручной ввод и подписи значений (DESIGN.md §9.4): запятая/точка, «−», «1.5k», «1,5 кГц». */
public static class EqFormat
{
    private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
    private static readonly CultureInfo Ru = CultureInfo.GetCultureInfo("ru-RU");

    private static string Clean(string s) => s.Trim().ToLowerInvariant()
        .Replace('−', '-').Replace('–', '-').Replace(',', '.').Replace(" ", "").Replace(" ", "").Replace(" ", "");

    public static double? ParseFreq(string input)
    {
        var s = Clean(input);
        var mul = 1.0;
        if (s.EndsWith("кгц") || s.EndsWith("khz")) { s = s[..^3]; mul = 1000; }
        else if (s.EndsWith("гц") || s.EndsWith("hz")) s = s[..^2];
        if (s.EndsWith('k') || s.EndsWith('к')) { s = s[..^1]; mul = 1000; }
        return double.TryParse(s, NumberStyles.Float, Inv, out var v) && double.IsFinite(v * mul) ? v * mul : null;
    }

    public static double? ParseNumber(string input, string unit = "")
    {
        var s = Clean(input);
        if (unit.Length > 0 && s.EndsWith(unit, StringComparison.Ordinal)) s = s[..^unit.Length];
        if (s.StartsWith('+')) s = s[1..];
        return double.TryParse(s, NumberStyles.Float, Inv, out var v) && double.IsFinite(v) ? v : null;
    }

    public static string Num(double v, int decimals) =>
        Math.Round(v, decimals, MidpointRounding.AwayFromZero).ToString("N" + decimals, Ru).Replace('-', '−').Replace(' ', ' ');

    public static string Freq(double f) => f < 100 && f != Math.Round(f) ? $"{Num(f, 1)} Гц" : $"{Num(f, 0)} Гц";
    public static string FreqShort(double f) => f >= 1000 ? Num(f / 1000, f % 1000 == 0 ? 0 : 1) + "k" : Num(f, 0);
    public static string Gain(double g) => (g > 0.04 ? "+" : "") + Num(g, 1) + " дБ";
    public static string Q(double q) => "Q " + Num(q, 2);
}
