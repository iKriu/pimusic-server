using System.Collections.ObjectModel;
using System.Collections.Specialized;
using System.ComponentModel;
using System.Net;
using System.Text.Json;
using System.Text.Json.Serialization;
using CommunityToolkit.Mvvm.ComponentModel;
using Microsoft.UI.Dispatching;
using PiMusic.Data;

namespace PiMusic.Player;

public enum OfflineState { Queued, Downloading, Done, Failed }

/** Скачанный (или скачиваемый) трек: метаданные трека, группа (альбом/плейлист, откуда скачали), файл и прогресс. */
public sealed partial class OfflineItem : ObservableObject
{
    public Track Track { get; set; } = new();
    public string? Group { get; set; }
    /** сервер, с которого скачан: id треков на другом сервере значат другие треки */
    public string? Server { get; set; }
    /** имя файла в папке offline (после завершения) */
    public string? File { get; set; }
    public long Updated { get; set; }
    [ObservableProperty] public partial OfflineState State { get; set; }
    [ObservableProperty] public partial long Bytes { get; set; }
    [ObservableProperty] public partial long Total { get; set; }

    [JsonIgnore] public bool Done => State == OfflineState.Done;
    [JsonIgnore] public bool Failed => State == OfflineState.Failed;
    [JsonIgnore] public bool Active => State is OfflineState.Queued or OfflineState.Downloading;
    /** 0–100; пока размер неизвестен — 0 */
    [JsonIgnore] public double Percent => Total > 0 ? Math.Clamp(Bytes * 100.0 / Total, 0, 100) : 0;
    [JsonIgnore] public string SizeText => State switch
    {
        OfflineState.Done => Fmt.Size(Bytes),
        OfflineState.Failed => "Не скачалось",
        OfflineState.Queued => "В очереди",
        _ => $"{Percent:0} %",
    };

    partial void OnStateChanged(OfflineState value)
    {
        OnPropertyChanged(nameof(Done));
        OnPropertyChanged(nameof(Failed));
        OnPropertyChanged(nameof(Active));
        OnPropertyChanged(nameof(SizeText));
    }

    partial void OnBytesChanged(long value)
    {
        OnPropertyChanged(nameof(Percent));
        OnPropertyChanged(nameof(SizeText));
    }

    partial void OnTotalChanged(long value) => OnPropertyChanged(nameof(Percent));
}

/** ObservableCollection со вставкой пачкой: одно уведомление Reset вместо N сдвигов списка. */
public sealed partial class OfflineItems : ObservableCollection<OfflineItem>
{
    public void InsertRange(int index, IReadOnlyList<OfflineItem> items)
    {
        if (items.Count == 0) return;
        CheckReentrancy();
        if (Items is List<OfflineItem> list) list.InsertRange(index, items);
        else for (var k = 0; k < items.Count; k++) Items.Insert(index + k, items[k]);
        OnPropertyChanged(new PropertyChangedEventArgs(nameof(Count)));
        OnPropertyChanged(new PropertyChangedEventArgs("Item[]"));
        OnCollectionChanged(new NotifyCollectionChangedEventArgs(NotifyCollectionChangedAction.Reset));
    }
}

/**
 * «Скачать на компьютер» (как OfflineDownloads.kt в Android): файл потока трека в %LOCALAPPDATA%\PiMusic\offline
 * (<id>.<расширение> + обложка <id>.jpg), индекс — offline\index.json. Хранится до ручного удаления; плеер играет
 * скачанное с диска (LocalUri). Очередь — по два файла одновременно, с прогрессом и отменой; 404 — ссылка истекла,
 * берём свежую через /api/tracks. 30 с без данных — обрыв («Не скачалось»). Все изменения состояния — в потоке
 * интерфейса (Init вызывается из него); индекс пишется с задержкой ~1 с в фоне, при выходе — Flush.
 */
public sealed class OfflineDownloads
{
    public static OfflineDownloads Instance { get; } = new();

    public static string Dir { get; } = Path.Combine(AppSettings.Dir, "offline");
    private static string IndexPath => Path.Combine(Dir, "index.json");
    private const int Parallel = 2;
    /** столько без единого байта — соединение считаем оборванным */
    private static readonly TimeSpan Idle = TimeSpan.FromSeconds(30);
    /** Media Foundation выбирает разборщик по расширению: только известные аудио */
    private static readonly HashSet<string> AudioExts = [".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".wma", ".aac"];

    /** Все элементы, новые сверху. */
    public OfflineItems Items { get; } = [];
    /** Добавили/удалили трек или он скачался/не скачался (значки «скачано», страница, параметры). */
    public event Action? Changed;

    private readonly Dictionary<long, OfflineItem> _byId = [];
    private readonly Dictionary<long, CancellationTokenSource> _running = [];
    private readonly HttpClient _http;

    private DispatcherQueueTimer? _saveTimer;
    private bool _dirty;
    /** номер записи: устаревшая фоновая запись не затирает более новую */
    private long _saveSeq;
    private long _writtenSeq;
    private readonly Lock _saveGate = new();
    private Task? _lastWrite;

    private OfflineDownloads()
    {
        _http = new HttpClient(new SocketsHttpHandler { ConnectTimeout = TimeSpan.FromSeconds(15) })
        {
            // общий тайм-аут не подходит большим файлам: обрыв ловит таймер бездействия (Idle)
            Timeout = Timeout.InfiniteTimeSpan,
        };
        _http.DefaultRequestHeaders.UserAgent.ParseAdd($"PiMusic-Windows/{Api.Version}");
    }

    /** Прочитать индекс; недокачанное — снова в очередь (если вход выполнен). */
    public void Init()
    {
        if (DispatcherQueue.GetForCurrentThread() is { } ui && _saveTimer is null)
        {
            _saveTimer = ui.CreateTimer();
            _saveTimer.Interval = TimeSpan.FromSeconds(1);
            _saveTimer.IsRepeating = false;
            _saveTimer.Tick += (_, _) => WriteIndex(sync: false);
        }
        var indexOk = true;
        try
        {
            if (System.IO.File.Exists(IndexPath))
            {
                List<JsonElement> raw;
                try { raw = JsonSerializer.Deserialize<List<JsonElement>>(System.IO.File.ReadAllText(IndexPath), Json.Options) ?? []; }
                catch (JsonException)
                {
                    // индекс повреждён целиком: не затираем молча — откладываем рядом и начинаем с пустого
                    indexOk = false;
                    try { System.IO.File.Move(IndexPath, IndexPath + ".bad", overwrite: true); } catch (Exception) { }
                    raw = [];
                }
                var list = new List<OfflineItem>();
                var skipped = 0;
                foreach (var el in raw)
                {
                    // битый элемент (например, "track": null) пропускаем, остальные читаем
                    try
                    {
                        if (el.Deserialize<OfflineItem>(Json.Options) is { Track: not null } o) list.Add(o);
                        else skipped++;
                    }
                    catch (Exception) { skipped++; }
                }
                foreach (var o in list.OrderByDescending(x => x.Updated))
                {
                    try
                    {
                        if (o.Track.Id <= 0 || _byId.ContainsKey(o.Track.Id)) continue;
                        // файл пропал (удалили руками) — элемент тоже
                        if (o.Done && (o.File is null || !System.IO.File.Exists(Path.Combine(Dir, o.File)))) continue;
                        if (o.State == OfflineState.Downloading) o.State = OfflineState.Queued;
                        FixLinks(o.Track);
                        _byId[o.Track.Id] = o;
                        Items.Add(o);
                    }
                    catch (Exception) { skipped++; }
                }
                if (skipped > 0)
                {
                    // часть индекса не разобралась (смена схемы?): копия рядом, файлы этих элементов не чистим
                    indexOk = false;
                    try { System.IO.File.Copy(IndexPath, IndexPath + ".bad", overwrite: true); } catch (Exception) { }
                }
            }
        }
        catch (Exception) { indexOk = false; /* диск недоступен — начинаем с пустого, файлы не трогаем */ }
        if (indexOk) CleanOrphans();
        if (Api.Instance.LoggedIn) Pump();
    }

    /** Возобновить очередь (после входа). */
    public void Resume()
    {
        if (Api.Instance.LoggedIn) Pump();
    }

    public bool IsDone(long id) => _byId.TryGetValue(id, out var o) && o.Done && Mine(o);

    private static bool Mine(OfflineItem o) => o.Server is null || o.Server == Api.Instance.Server;

    /** Файл скачанного трека (file:///…) или null — играть из сети. */
    public Uri? LocalUri(long id)
    {
        if (!_byId.TryGetValue(id, out var o) || !o.Done || o.File is null || !Mine(o)) return null;
        var path = Path.Combine(Dir, o.File);
        return System.IO.File.Exists(path) ? new Uri(path) : null;
    }

    /** Места на диске под скачанное (байт). */
    public long DiskBytes => Items.Where(o => o.Done).Sum(o => o.Bytes);
    public int DoneCount => Items.Count(o => o.Done);
    public int ActiveCount => Items.Count(o => o.Active);

    /** Поставить в скачивание; уже скачанные и скачиваемые пропускаются. Возвращает, сколько добавлено. */
    public int Add(IEnumerable<Track> tracks, string? group)
    {
        var n = 0;
        var added = new List<OfflineItem>();
        foreach (var t in tracks)
        {
            if (t.Id <= 0 || t.Live) continue;
            if (_byId.TryGetValue(t.Id, out var old))
            {
                if (!Mine(old))
                {
                    // тот же id с другого сервера — другой трек: старый убираем вместе с файлами
                    Drop(old);
                }
                else
                {
                    if (!old.Failed) continue;
                    old.Track = t;
                    old.Bytes = 0;
                    old.State = OfflineState.Queued;
                    n++;
                    continue;
                }
            }
            var o = new OfflineItem { Track = t, Group = group, Server = Api.Instance.Server, Updated = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() };
            _byId[t.Id] = o;
            added.Add(o);
            n++;
        }
        // каждый следующий — выше предыдущего (последний добавленный сверху, Pump качает первые первыми);
        // большая пачка — одной вставкой в том же порядке
        if (added.Count > 8)
        {
            added.Reverse();
            Items.InsertRange(0, added);
        }
        else foreach (var o in added) Items.Insert(0, o);
        if (n > 0)
        {
            Save();
            Pump();
            Notify();
        }
        return n;
    }

    public void Retry(OfflineItem o) => Add([o.Track], o.Group);

    /** Удалить с компьютера (скачиваемый — отменить). */
    public void Remove(long id)
    {
        if (!_byId.TryGetValue(id, out var o)) return;
        Drop(o);
        Save();
        Pump();
        Notify();
    }

    /** Убрать элемент из списка и его файлы (скачиваемый — отменить). */
    private void Drop(OfflineItem o)
    {
        var id = o.Track.Id;
        _byId.Remove(id);
        if (_running.Remove(id, out var cts)) cts.Cancel();
        Items.Remove(o);
        DeleteFiles(id, o.File);
    }

    /** Удалить всё скачанное; папку чистит и при пустом списке (остатки прошлых версий, недокачанное). */
    public void RemoveAll()
    {
        foreach (var cts in _running.Values) cts.Cancel();
        _running.Clear();
        _byId.Clear();
        Items.Clear();
        try
        {
            if (Directory.Exists(Dir))
                foreach (var f in Directory.EnumerateFiles(Dir))
                    try { System.IO.File.Delete(f); } catch (Exception) { /* играет прямо сейчас — останется до следующей очистки */ }
        }
        catch (Exception) { }
        Save();
        Notify();
    }

    /** Подписчиков — по одному: ошибка одного не мешает остальным и очереди. */
    private void Notify()
    {
        if (Changed is not { } h) return;
        foreach (var d in h.GetInvocationList())
        {
            try { ((Action)d)(); }
            catch (Exception) { }
        }
    }

    // ---------------------------------------------------------------- очередь

    /** Запустить следующие из очереди (старые первыми), пока занято меньше Parallel. Чужой сервер — пропускаем. */
    private void Pump()
    {
        while (_running.Count < Parallel
               && Items.LastOrDefault(o => o.State == OfflineState.Queued && Mine(o) && !_running.ContainsKey(o.Track.Id)) is { } next)
        {
            var cts = new CancellationTokenSource();
            _running[next.Track.Id] = cts;
            _ = Run(next, cts);
        }
    }

    private async Task Run(OfflineItem o, CancellationTokenSource cts)
    {
        var id = o.Track.Id;
        try
        {
            o.Bytes = 0;
            o.Total = 0;
            o.State = OfflineState.Downloading;
            var ok = false;
            // отмена cts — удалили; любая другая ошибка (сеть, диск, 30 с без данных) — «Не скачалось», можно повторить
            try { ok = await Fetch(o, cts.Token); }
            catch (Exception) { }
            if (_running.TryGetValue(id, out var cur) && ReferenceEquals(cur, cts)) _running.Remove(id);
            if (cts.IsCancellationRequested || !ReferenceEquals(_byId.GetValueOrDefault(id), o))
            {
                // удалили во время скачивания: убрать успевший появиться файл, если под этим id нет нового элемента
                if (!_byId.ContainsKey(id)) DeleteFiles(id, o.File);
                return;
            }
            if (!ok) DeleteFiles(id, null);
            o.Updated = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            o.State = ok ? OfflineState.Done : OfflineState.Failed;
            Save();
            Pump();
            Notify();
        }
        finally { cts.Dispose(); }
    }

    /** Скачать поток трека (при 404 — один раз со свежей ссылкой) и обложку. true — файл на месте. */
    private async Task<bool> Fetch(OfflineItem o, CancellationToken ct)
    {
        var id = o.Track.Id;
        Directory.CreateDirectory(Dir);
        for (var attempt = 0; attempt < 2; attempt++)
        {
            if (o.Track.StreamUri is null || attempt > 0)
            {
                var fresh = (await Api.Instance.Tracks([id])).Items.FirstOrDefault(x => x.Id == id);
                ct.ThrowIfCancellationRequested();
                if (fresh?.StreamUri is null) return false;
                o.Track = fresh;
            }
            var uri = o.Track.StreamUri!;
            // своё имя у каждой загрузки: удаление и повторное добавление не делят один файл
            var part = Path.Combine(Dir, $"{id}.{Guid.NewGuid():N}.part");
            using var idle = CancellationTokenSource.CreateLinkedTokenSource(ct);
            idle.CancelAfter(Idle);
            try
            {
                using var resp = await _http.GetAsync(uri, HttpCompletionOption.ResponseHeadersRead, idle.Token);
                if (resp.StatusCode == HttpStatusCode.NotFound && attempt == 0) continue;
                if (!resp.IsSuccessStatusCode) return false;
                // страница ошибки прокси (200 text/html) — не трек
                var media = resp.Content.Headers.ContentType?.MediaType?.ToLowerInvariant();
                if (!string.IsNullOrEmpty(media) && !media.StartsWith("audio/", StringComparison.Ordinal)
                    && media is not ("application/octet-stream" or "binary/octet-stream" or "application/ogg" or "video/mp4" or "video/webm"))
                    return false;
                var expected = resp.Content.Headers.ContentLength ?? 0;
                o.Total = expected;
                var file = id + Ext(media, uri);
                IProgress<long> progress = new Progress<long>(b => o.Bytes = b);
                var got = await Task.Run(async () =>
                {
                    await using var src = await resp.Content.ReadAsStreamAsync(idle.Token);
                    await using var dst = new FileStream(part, FileMode.Create, FileAccess.Write, FileShare.None, 1 << 16, useAsync: true);
                    var buf = new byte[1 << 16];
                    long done = 0;
                    var last = Environment.TickCount64;
                    int n;
                    while ((n = await src.ReadAsync(buf, idle.Token)) > 0)
                    {
                        idle.CancelAfter(Idle);
                        await dst.WriteAsync(buf.AsMemory(0, n), idle.Token);
                        done += n;
                        if (Environment.TickCount64 - last >= 250)
                        {
                            last = Environment.TickCount64;
                            progress.Report(done);
                        }
                    }
                    return done;
                }, idle.Token);
                if (got <= 0 || (expected > 0 && got != expected)) return false;
                // отмена после копирования — файл под постоянным именем не создаём
                ct.ThrowIfCancellationRequested();
                System.IO.File.Move(part, Path.Combine(Dir, file), overwrite: true);
                o.File = file;
                o.Bytes = got;
            }
            finally
            {
                // после переноса .part уже нет; при ошибке/отмене — убрать недокачанное
                try { System.IO.File.Delete(part); } catch (Exception) { }
            }
            await FetchCover(o.Track, ct);
            return true;
        }
        return false;
    }

    /** Обложка рядом с файлом — чтобы и без сети в списках была картинка (необязательно). */
    private async Task FetchCover(Track t, CancellationToken ct)
    {
        if (t.CoverUri is not { } uri || uri.IsFile) return;
        try
        {
            using var idle = CancellationTokenSource.CreateLinkedTokenSource(ct);
            idle.CancelAfter(Idle);
            var bytes = await _http.GetByteArrayAsync(uri, idle.Token);
            var path = Path.Combine(Dir, t.Id + ".jpg");
            await System.IO.File.WriteAllBytesAsync(path, bytes, ct);
            t.CoverUri = new Uri(path);
        }
        catch (Exception) when (!ct.IsCancellationRequested) { /* без обложки — заглушка */ }
    }

    /** Расширение файла по типу ответа (Media Foundation выбирает разборщик по нему), иначе по адресу; не аудио — .mp3. */
    private static string Ext(string? mediaType, Uri uri) => mediaType switch
    {
        "audio/mpeg" or "audio/mp3" => ".mp3",
        "audio/flac" or "audio/x-flac" => ".flac",
        "audio/mp4" or "audio/x-m4a" or "audio/m4a" or "audio/aac" or "video/mp4" => ".m4a",
        "audio/ogg" or "audio/vorbis" or "application/ogg" => ".ogg",
        "audio/opus" => ".opus",
        "audio/wav" or "audio/x-wav" or "audio/wave" => ".wav",
        "audio/x-ms-wma" => ".wma",
        _ => Path.GetExtension(uri.AbsolutePath).ToLowerInvariant() is var e && AudioExts.Contains(e) ? e : ".mp3",
    };

    // ---------------------------------------------------------------- файлы

    private static void FixLinks(Track t)
    {
        t.StreamUri = Api.Instance.Abs(t.Stream);
        var cover = Path.Combine(Dir, t.Id + ".jpg");
        t.CoverUri = System.IO.File.Exists(cover) ? new Uri(cover) : Api.Instance.Abs(t.Cover);
    }

    private static void DeleteFiles(long id, string? file)
    {
        foreach (var name in new[] { file, id + ".jpg" })
        {
            if (name is null) continue;
            try { System.IO.File.Delete(Path.Combine(Dir, name)); }
            catch (Exception) { /* файл играет прямо сейчас — останется до очистки */ }
        }
        try
        {
            foreach (var part in Directory.EnumerateFiles(Dir, $"{id}.*.part"))
                try { System.IO.File.Delete(part); } catch (Exception) { }
        }
        catch (Exception) { }
    }

    /** После чтения индекса: убрать <id>.* без записи в индексе (и лишние файлы элемента) и старые .part. */
    private void CleanOrphans()
    {
        var keep = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var o in Items)
        {
            if (o.File is { } f) keep.Add(f);
            keep.Add(o.Track.Id + ".jpg");
        }
        // список — сейчас, до Pump: новые .part (своё имя у каждой загрузки) в него не попадут
        var orphans = new List<string>();
        try
        {
            if (!Directory.Exists(Dir)) return;
            foreach (var path in Directory.EnumerateFiles(Dir))
            {
                var name = Path.GetFileName(path);
                if (name.StartsWith("index.json", StringComparison.OrdinalIgnoreCase)) continue;
                var dot = name.IndexOf('.');
                if (name.EndsWith(".part", StringComparison.OrdinalIgnoreCase)
                    || (dot > 0 && long.TryParse(name.AsSpan(0, dot), out var id) && (!_byId.ContainsKey(id) || !keep.Contains(name))))
                    orphans.Add(path);
            }
        }
        catch (Exception) { return; }
        if (orphans.Count == 0) return;
        _ = Task.Run(() =>
        {
            foreach (var path in orphans)
                try { System.IO.File.Delete(path); } catch (Exception) { }
        });
    }

    /** Отметить индекс изменённым: запись через ~1 с (пачка изменений — одна запись). */
    private void Save()
    {
        _dirty = true;
        if (_saveTimer is null) { WriteIndex(sync: true); return; }
        if (!_saveTimer.IsRunning) _saveTimer.Start();
    }

    /** При выходе: записать индекс сразу, если есть несохранённое. */
    public void Flush()
    {
        WriteIndex(sync: true);
        // фоновая запись могла уйти раньше и ещё идти — не выходим посреди неё
        try { _lastWrite?.Wait(TimeSpan.FromSeconds(5)); } catch (Exception) { }
    }

    /** Сериализация — здесь (поток интерфейса), запись файла — в фоне атомарно (или сразу при sync). */
    private void WriteIndex(bool sync)
    {
        _saveTimer?.Stop();
        if (!_dirty) return;
        _dirty = false;
        string json;
        try { json = JsonSerializer.Serialize(Items.ToList(), Json.Options); }
        catch (Exception) { return; }
        var seq = ++_saveSeq;
        if (sync) WriteFile(json, seq);
        else _lastWrite = Task.Run(() => WriteFile(json, seq));
    }

    private void WriteFile(string json, long seq)
    {
        lock (_saveGate)
        {
            if (seq <= _writtenSeq) return;
            try
            {
                Directory.CreateDirectory(Dir);
                var tmp = IndexPath + ".tmp";
                System.IO.File.WriteAllText(tmp, json);
                System.IO.File.Move(tmp, IndexPath, overwrite: true);
                _writtenSeq = seq;
            }
            catch (Exception) { /* запишем при следующей смене */ }
        }
    }
}
