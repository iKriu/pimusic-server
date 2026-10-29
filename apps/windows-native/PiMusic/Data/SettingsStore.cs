using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PiMusic.Data;

/**
 * Настройки между устройствами (порт static/js/settings.js и SettingsStore Android): запись — сразу локально
 * (%LOCALAPPDATA%\PiMusic\sync-<логин>.json), в фоне — PUT /api/settings/<ключ> с base. 409 conflict → значение
 * сервера выигрывает (current: null — base = 0 и повтор); 409 limit / 400 / 413 → ключ «паркуется»; сеть/5xx/429 —
 * повтор позже. Changed приходит в фоновом потоке: (ключ, пришло с сервера).
 */
public sealed partial class SettingsStore
{
    private sealed class Item
    {
        public JsonElement? Value { get; set; }
        public long UpdatedAt { get; set; }
        public long Base { get; set; }
        public bool Dirty { get; set; }
        public bool Parked { get; set; }
    }

    [GeneratedRegex("^[a-z0-9._-]{1,80}$")]
    private static partial Regex KeyRe();

    private readonly Lock _gate = new();
    private readonly SemaphoreSlim _push = new(1, 1);
    private Dictionary<string, Item> _items = [];
    private string _login = "";
    private CancellationTokenSource? _pushTimer;

    // после полей: конструктор и инициализаторы выше должны быть готовы
    public static SettingsStore Instance { get; } = new();

    public event Action<string, bool>? Changed;

    private SettingsStore() { }

    private string FilePath => Path.Combine(AppSettings.Dir, $"sync-{string.Concat(_login.Where(char.IsLetterOrDigit))}.json");

    /** Сменился вход: своё хранилище у каждого логина. */
    public void SetLogin(string? login)
    {
        lock (_gate)
        {
            _login = login ?? "";
            _items = [];
            if (_login.Length == 0) return;
            try
            {
                if (File.Exists(FilePath))
                    _items = JsonSerializer.Deserialize<Dictionary<string, Item>>(File.ReadAllText(FilePath), Json.Options) ?? [];
            }
            catch (Exception) { _items = []; }
        }
    }

    private void Persist()
    {
        if (_login.Length == 0) return;
        try
        {
            Directory.CreateDirectory(AppSettings.Dir);
            var tmp = FilePath + ".tmp";
            File.WriteAllText(tmp, JsonSerializer.Serialize(_items, Json.Options));
            File.Move(tmp, FilePath, overwrite: true);
        }
        catch (Exception) { /* диск недоступен — сохранится при следующей записи */ }
    }

    public JsonElement? Get(string key)
    {
        lock (_gate)
            return _items.TryGetValue(key, out var i) && i.Value is { ValueKind: not JsonValueKind.Null and not JsonValueKind.Undefined } v ? v : null;
    }

    public T? Get<T>(string key) where T : class
    {
        if (Get(key) is not { } v) return null;
        try { return v.Deserialize<T>(Json.Options); }
        catch (Exception) { return null; }
    }

    public List<string> Keys(string prefix)
    {
        lock (_gate)
            return [.. _items.Where(p => p.Key.StartsWith(prefix, StringComparison.Ordinal)
                && p.Value.Value is { ValueKind: not JsonValueKind.Null and not JsonValueKind.Undefined }).Select(p => p.Key)];
    }

    public void Set<T>(string key, T value) => Set(key, JsonSerializer.SerializeToElement(value, Json.Options));

    public void Set(string key, JsonElement value)
    {
        if (!KeyRe().IsMatch(key)) throw new ArgumentException("bad key " + key);
        lock (_gate)
        {
            var b = _items.TryGetValue(key, out var cur) ? cur.Base : 0;
            _items[key] = new Item { Value = value, UpdatedAt = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(), Base = b, Dirty = true };
            Persist();
        }
        Changed?.Invoke(key, false);
        Schedule(TimeSpan.FromMilliseconds(1200));
    }

    private void Schedule(TimeSpan delay)
    {
        CancellationTokenSource cts;
        lock (_gate)
        {
            _pushTimer?.Cancel();
            _pushTimer = cts = new CancellationTokenSource();
        }
        _ = Task.Run(async () =>
        {
            try { await Task.Delay(delay, cts.Token); }
            catch (OperationCanceledException) { return; }
            await PushAll();
        });
    }

    /** Отправить несохранённое; true — что-то нужно повторить позже. */
    public async Task<bool> PushAll()
    {
        if (_login.Length == 0 || !Api.Instance.LoggedIn) return false;
        var retry = false;
        await _push.WaitAsync();
        try
        {
            List<string> dirty;
            lock (_gate) dirty = [.. _items.Where(p => p.Value.Dirty && !p.Value.Parked).Select(p => p.Key)];
            foreach (var key in dirty)
            {
                Item it;
                lock (_gate)
                {
                    if (!_items.TryGetValue(key, out var x)) continue;
                    it = x;
                }
                var sent = it.UpdatedAt;
                try
                {
                    var value = it.Value ?? JsonSerializer.SerializeToElement<object?>(null);
                    var r = await Api.Instance.PutSetting(key, value, it.Base > 0 ? it.Base : null);
                    lock (_gate)
                    {
                        if (!_items.TryGetValue(key, out var cur)) continue;
                        if (r.UpdatedAt > 0) cur.Base = r.UpdatedAt;
                        // за время отправки ключ могли изменить ещё раз — тогда он остаётся «грязным»
                        cur.Dirty = cur.Dirty && cur.UpdatedAt != sent;
                    }
                }
                catch (ApiException e) when (e.Status == 409 && e.Code == "conflict")
                {
                    if (e.Body is { } body && body.TryGetProperty("current", out var c) && c.ValueKind == JsonValueKind.Object)
                    {
                        var t = c.TryGetProperty("updated_at", out var u) && u.TryGetInt64(out var ut) ? ut : 0;
                        bool taken;
                        lock (_gate)
                        {
                            // пока шёл запрос, ключ изменили здесь ещё раз — новое значение уйдёт с base сервера
                            taken = !_items.TryGetValue(key, out var cur) || cur.UpdatedAt == sent;
                            if (taken) _items[key] = new Item { Value = c.TryGetProperty("value", out var v) ? v.Clone() : null, UpdatedAt = t, Base = t };
                            else cur!.Base = t;
                        }
                        if (taken) Changed?.Invoke(key, true);
                        else retry = true;
                    }
                    else
                    {
                        lock (_gate) if (_items.TryGetValue(key, out var cur)) cur.Base = 0;
                        retry = true;
                    }
                }
                catch (ApiException e) when (e.Status is 409 or 400 or 413)
                {
                    lock (_gate) if (_items.TryGetValue(key, out var cur)) cur.Parked = true;
                }
                catch (Exception) { retry = true; }
            }
            lock (_gate) Persist();
        }
        finally { _push.Release(); }
        if (retry) Schedule(TimeSpan.FromSeconds(15));
        return retry;
    }

    /** Забрать с сервера ключи, которые здесь не правились и на сервере новее. */
    public async Task<bool> Sync()
    {
        if (_login.Length == 0 || !Api.Instance.LoggedIn) return false;
        SettingsAll remote;
        try { remote = await Api.Instance.Settings(); }
        catch (Exception) { return false; }
        var changed = new List<string>();
        bool dirtyLeft;
        lock (_gate)
        {
            foreach (var (key, s) in remote.Items)
            {
                if (!KeyRe().IsMatch(key)) continue;
                if (!_items.TryGetValue(key, out var cur) || (!cur.Dirty && s.UpdatedAt > cur.Base))
                {
                    _items[key] = new Item { Value = s.Value?.Clone(), UpdatedAt = s.UpdatedAt, Base = s.UpdatedAt };
                    changed.Add(key);
                }
            }
            Persist();
            dirtyLeft = _items.Values.Any(i => i.Dirty && !i.Parked);
        }
        foreach (var k in changed) Changed?.Invoke(k, true);
        if (dirtyLeft) _ = PushAll();
        return true;
    }

    /** Копия значения ключа как JsonObject (для правки), если там объект. */
    public JsonObject GetObject(string key) =>
        Get(key) is { ValueKind: JsonValueKind.Object } v ? JsonNode.Parse(v.GetRawText())!.AsObject() : [];
}
