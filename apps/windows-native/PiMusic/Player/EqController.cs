using System.Text.Json;
using System.Text.Json.Serialization;
using PiMusic.Data;

namespace PiMusic.Player;

/**
 * Эквалайзер из настроек — тот же формат ключей, что у eq.js и Android (EqController.kt):
 *   eq.presets — свои пресеты [{id, name, preamp, bands}]; eq.shared — общий профиль {active, preamp, bands};
 *   eq.device.<id> — это устройство {name, platform, mode: sync|local, active, preamp, bands, enabled, limiter, …}.
 * В режиме sync полосы и преамп берутся из eq.shared (одинаково на всех устройствах), в local — свои.
 * Незнакомые поля других клиентов сохраняются (JsonExtensionData). Changed — в потоке интерфейса.
 */
public sealed class EqController
{
    public sealed class JDevice
    {
        public string Name { get; set; } = "";
        public string Platform { get; set; } = "windows";
        public string Mode { get; set; } = "sync";
        public string Active { get; set; } = "flat";
        public double Preamp { get; set; }
        public List<Band>? Bands { get; set; }
        public bool Enabled { get; set; } = true;
        public bool Limiter { get; set; } = true;
        [JsonExtensionData] public Dictionary<string, JsonElement>? Extra { get; set; }
    }

    public sealed class JShared
    {
        public string Active { get; set; } = "flat";
        public double Preamp { get; set; }
        public List<Band>? Bands { get; set; }
        [JsonExtensionData] public Dictionary<string, JsonElement>? Extra { get; set; }
    }

    public sealed class JPreset
    {
        public string Id { get; set; } = "";
        public string Name { get; set; } = "";
        public double Preamp { get; set; }
        public List<Band>? Bands { get; set; }
        [JsonExtensionData] public Dictionary<string, JsonElement>? Extra { get; set; }
    }

    private readonly SettingsStore _s = SettingsStore.Instance;
    private Microsoft.UI.Dispatching.DispatcherQueue? _ui;

    // после полей
    public static EqController Instance { get; } = new();

    /** Профиль или список пресетов изменился (здесь или пришло с другого устройства). */
    public event Action? Changed;

    private EqController() { }

    public static string DeviceId
    {
        get
        {
            var a = AppSettings.Current;
            if (string.IsNullOrEmpty(a.DeviceId))
            {
                a.DeviceId = "win-" + Guid.NewGuid().ToString("N")[..12];
                try { a.Save(); } catch (Exception) { }
            }
            return a.DeviceId!;
        }
    }

    private static string DevKey => "eq.device." + DeviceId;
    private static string DeviceName => "Windows · " + Environment.MachineName;

    /** Вызвать при старте из потока интерфейса (после входа — Refresh). */
    public void Init(Microsoft.UI.Dispatching.DispatcherQueue ui)
    {
        _ui = ui;
        _s.Changed += (key, _) =>
        {
            if (key.StartsWith("eq.", StringComparison.Ordinal)) _ui?.TryEnqueue(Apply);
        };
        Apply();
    }

    /**
     * После синхронизации: применить; synced — настройки сервера получены, и если этого устройства там нет,
     * записать его (имя видно на других). Без синхронизации не пишем: не затереть запись, которой здесь не видно.
     */
    public void Refresh(bool synced)
    {
        if (synced && _s.Get(DevKey) is null && Api.Instance.LoggedIn) WriteDevice(Defaults());
        Apply();
    }

    private static JDevice Defaults() => new() { Name = DeviceName, Bands = Eq.FlatBands() };

    public JDevice Device() => _s.Get<JDevice>(DevKey) ?? Defaults();
    private JShared Shared() => _s.Get<JShared>("eq.shared") ?? new JShared { Bands = Eq.FlatBands() };

    public List<Preset> CustomPresets() =>
        [.. (_s.Get<List<JPreset>>("eq.presets") ?? []).Where(p => p.Id.Length > 0)
            .Select(p => new Preset(p.Id, p.Name, Math.Clamp(p.Preamp, Eq.PreMin, Eq.PreMax), Eq.Sanitize(p.Bands)))];

    public List<Preset> AllPresets() => [.. Eq.Builtin, .. CustomPresets()];

    /** Действующий профиль: (активный пресет, состояние). */
    public (string Active, EqState State) Effective()
    {
        var d = Device();
        string active = d.Active;
        var pre = d.Preamp;
        var bands = d.Bands;
        if (d.Mode == "sync")
        {
            var s = Shared();
            (active, pre, bands) = (s.Active, s.Preamp, s.Bands);
        }
        return (active, new EqState(d.Enabled, Math.Clamp(pre, Eq.PreMin, Eq.PreMax), Eq.Sanitize(bands), d.Limiter));
    }

    private void Apply()
    {
        PlayerService.Instance.ApplyEq(Effective().State);
        Changed?.Invoke();
    }

    private void WriteDevice(JDevice d) => _s.Set(DevKey, d);

    private void EditProfile(string? active = null, double? preamp = null, IReadOnlyList<Band>? bands = null)
    {
        var d = Device();
        if (d.Mode == "sync")
        {
            var s = Shared();
            s.Active = active ?? s.Active;
            s.Preamp = preamp ?? s.Preamp;
            s.Bands = bands is null ? s.Bands : [.. bands];
            _s.Set("eq.shared", s);
        }
        else
        {
            d.Active = active ?? d.Active;
            d.Preamp = preamp ?? d.Preamp;
            d.Bands = bands is null ? d.Bands : [.. bands];
            WriteDevice(d);
        }
        Apply();
    }

    public void SetBand(int i, Band band)
    {
        var bands = Effective().State.Bands.ToList();
        if (i < 0 || i >= bands.Count) return;
        bands[i] = band;
        EditProfile("custom", bands: Eq.Sanitize(bands));
    }

    public void SetPreamp(double v) => EditProfile("custom", preamp: Math.Clamp(Math.Round(v * 10) / 10, Eq.PreMin, Eq.PreMax));

    public void ApplyPreset(string id)
    {
        if (AllPresets().FirstOrDefault(p => p.Id == id) is not { } p) return;
        EditProfile(p.Id, p.Preamp, p.Bands);
    }

    public void SetEnabled(bool on) { var d = Device(); d.Enabled = on; WriteDevice(d); Apply(); }
    public void SetLimiter(bool on) { var d = Device(); d.Limiter = on; WriteDevice(d); Apply(); }

    /** Смена режима: в local переносим текущий общий профиль, чтобы звук не прыгнул. */
    public void SetMode(string mode)
    {
        var d = Device();
        if (d.Mode == mode) return;
        if (mode == "local")
        {
            var s = Shared();
            d.Active = s.Active;
            d.Preamp = s.Preamp;
            d.Bands = s.Bands;
        }
        d.Mode = mode;
        WriteDevice(d);
        Apply();
    }

    private static string NewId() => "u" + DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString("x") + Random.Shared.Next(0, 9999).ToString("x");

    public void SavePreset(string name)
    {
        var st = Effective().State;
        var list = _s.Get<List<JPreset>>("eq.presets") ?? [];
        var item = new JPreset
        {
            Id = NewId(),
            Name = string.IsNullOrWhiteSpace(name) ? "Мой пресет" : name.Trim()[..Math.Min(40, name.Trim().Length)],
            Preamp = st.Preamp,
            Bands = [.. st.Bands],
        };
        list.Add(item);
        _s.Set("eq.presets", list);
        EditProfile(item.Id);
    }

    public void RenamePreset(string id, string name)
    {
        var list = _s.Get<List<JPreset>>("eq.presets") ?? [];
        foreach (var p in list.Where(p => p.Id == id && !string.IsNullOrWhiteSpace(name))) p.Name = name.Trim()[..Math.Min(40, name.Trim().Length)];
        _s.Set("eq.presets", list);
        Apply();
    }

    public void DeletePreset(string id)
    {
        var list = _s.Get<List<JPreset>>("eq.presets") ?? [];
        list.RemoveAll(p => p.Id == id);
        _s.Set("eq.presets", list);
        Apply();
    }
}
