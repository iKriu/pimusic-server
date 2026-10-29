using System.Collections.ObjectModel;
using System.Net;
using System.Text.Json;
using System.Text.Json.Serialization;
using CommunityToolkit.Mvvm.ComponentModel;
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

/**
 * «Скачать на компьютер» (как OfflineDownloads.kt в Android): файл потока трека в %LOCALAPPDATA%\PiMusic\offline
 * (<id>.<расширение> + обложка <id>.jpg), индекс — offline\index.json. Хранится до ручного удаления; плеер играет
 * скачанное с диска (LocalUri). Очередь — по два файла одновременно, с прогрессом и отменой; 404 — ссылка истекла,
 * берём свежую через /api/tracks. Все изменения состояния — в потоке интерфейса (Init вызывается из него).
 */
public sealed class OfflineDownloads
{
    public static OfflineDownloads Instance { get; } = new();

    public static string Dir { get; } = Path.Combine(AppSettings.Dir, "offline");
    private static string IndexPath => Path.Combine(Dir, "index.json");
    private const int Parallel = 2;

    /** Все элементы, новые сверху. */
    public ObservableCollection<OfflineItem> Items { get; } = [];
    /** Добавили/удалили трек или он скачался/не скачался (значки «скачано», страница, параметры). */
    public event Action? Changed;

    private readonly Dictionary<long, OfflineItem> _byId = [];
    private readonly Dictionary<long, CancellationTokenSource> _running = [];
    private readonly HttpClient _http;

    private OfflineDownloads()
    {
        _http = new HttpClient(new SocketsHttpHandler { ConnectTimeout = TimeSpan.FromSeconds(15) })
        {
            Timeout = Timeout.InfiniteTimeSpan,
        };
        _http.DefaultRequestHeaders.UserAgent.ParseAdd($"PiMusic-Windows/{Api.Version}");
    }

    /** Прочитать индекс; недокачанное — снова в очередь (если вход выполнен). */
    public void Init()
    {
        try
        {
            if (System.IO.File.Exists(IndexPath))
            {
                var list = JsonSerializer.Deserialize<List<OfflineItem>>(System.IO.File.ReadAllText(IndexPath), Json.Options) ?? [];
                foreach (var o in list.OrderByDescending(x => x.Updated))
                {
                    if (o.Track.Id <= 0 || _byId.ContainsKey(o.Track.Id)) continue;
                    // файл пропал (удалили руками) — элемент тоже
                    if (o.Done && (o.File is null || !System.IO.File.Exists(Path.Combine(Dir, o.File)))) continue;
                    if (o.State == OfflineState.Downloading) o.State = OfflineState.Queued;
                    FixLinks(o.Track);
                    _byId[o.Track.Id] = o;
                    Items.Add(o);
                }
            }
        }
        catch (Exception) { /* индекс повреждён — начинаем с пустого */ }
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
        foreach (var t in tracks)
        {
            if (t.Id <= 0 || t.Live) continue;
            if (_byId.TryGetValue(t.Id, out var old))
            {
                if (!old.Failed) continue;
                old.Track = t;
                old.Bytes = 0;
                old.State = OfflineState.Queued;
                n++;
                continue;
            }
            var o = new OfflineItem { Track = t, Group = group, Server = Api.Instance.Server, Updated = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() };
            _byId[t.Id] = o;
            Items.Insert(0, o);
            n++;
        }
        if (n > 0)
        {
            Save();
            Changed?.Invoke();
            Pump();
        }
        return n;
    }

    public void Retry(OfflineItem o) => Add([o.Track], o.Group);

    /** Удалить с компьютера (скачиваемый — отменить). */
    public void Remove(long id)
    {
        if (!_byId.Remove(id, out var o)) return;
        if (_running.Remove(id, out var cts)) cts.Cancel();
        Items.Remove(o);
        DeleteFiles(id, o.File);
        Save();
        Changed?.Invoke();
        Pump();
    }

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
        Changed?.Invoke();
    }

    // ---------------------------------------------------------------- очередь

    /** Запустить следующие из очереди (старые первыми), пока занято меньше Parallel. */
    private void Pump()
    {
        while (_running.Count < Parallel
               && Items.LastOrDefault(o => o.State == OfflineState.Queued && !_running.ContainsKey(o.Track.Id)) is { } next)
        {
            var cts = new CancellationTokenSource();
            _running[next.Track.Id] = cts;
            _ = Run(next, cts);
        }
    }

    private async Task Run(OfflineItem o, CancellationTokenSource cts)
    {
        var id = o.Track.Id;
        o.Bytes = 0;
        o.Total = 0;
        o.State = OfflineState.Downloading;
        var ok = false;
        try { ok = await Fetch(o, cts.Token); }
        catch (Exception) { /* сеть, диск — «Не скачалось», можно повторить */ }
        // удалили во время скачивания: файлы уже убраны (или уберутся при следующей очистке)
        if (cts.IsCancellationRequested || !ReferenceEquals(_byId.GetValueOrDefault(id), o))
        {
            DeleteFiles(id, null);
            return;
        }
        _running.Remove(id);
        if (!ok) DeleteFiles(id, null);
        o.Updated = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        o.State = ok ? OfflineState.Done : OfflineState.Failed;
        Save();
        Changed?.Invoke();
        Pump();
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
            using var resp = await _http.GetAsync(uri, HttpCompletionOption.ResponseHeadersRead, ct);
            if (resp.StatusCode == HttpStatusCode.NotFound && attempt == 0) continue;
            if (!resp.IsSuccessStatusCode) return false;
            var total = resp.Content.Headers.ContentLength ?? 0;
            o.Total = total;
            var file = id + Ext(resp.Content.Headers.ContentType?.MediaType, uri);
            var part = Path.Combine(Dir, id + ".part");
            IProgress<long> progress = new Progress<long>(b => o.Bytes = b);
            var got = await Task.Run(async () =>
            {
                await using var src = await resp.Content.ReadAsStreamAsync(ct);
                await using var dst = new FileStream(part, FileMode.Create, FileAccess.Write, FileShare.None, 1 << 16, useAsync: true);
                var buf = new byte[1 << 16];
                long done = 0;
                var last = Environment.TickCount64;
                int n;
                while ((n = await src.ReadAsync(buf, ct)) > 0)
                {
                    await dst.WriteAsync(buf.AsMemory(0, n), ct);
                    done += n;
                    if (Environment.TickCount64 - last >= 250)
                    {
                        last = Environment.TickCount64;
                        progress.Report(done);
                    }
                }
                return done;
            }, ct);
            if (total > 0 && got != total) return false;
            System.IO.File.Move(part, Path.Combine(Dir, file), overwrite: true);
            o.File = file;
            o.Bytes = got;
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
            var bytes = await _http.GetByteArrayAsync(uri, ct);
            var path = Path.Combine(Dir, t.Id + ".jpg");
            await System.IO.File.WriteAllBytesAsync(path, bytes, ct);
            t.CoverUri = new Uri(path);
        }
        catch (Exception) when (!ct.IsCancellationRequested) { /* без обложки — заглушка */ }
    }

    /** Расширение файла по типу ответа (Media Foundation выбирает разборщик по нему), иначе по адресу. */
    private static string Ext(string? mediaType, Uri uri) => mediaType?.ToLowerInvariant() switch
    {
        "audio/mpeg" or "audio/mp3" => ".mp3",
        "audio/flac" or "audio/x-flac" => ".flac",
        "audio/mp4" or "audio/x-m4a" or "audio/m4a" or "audio/aac" => ".m4a",
        "audio/ogg" or "audio/vorbis" => ".ogg",
        "audio/opus" => ".opus",
        "audio/wav" or "audio/x-wav" or "audio/wave" => ".wav",
        "audio/x-ms-wma" => ".wma",
        _ => Path.GetExtension(uri.AbsolutePath).ToLowerInvariant() is { Length: > 1 and <= 6 } e && e is not (".part" or ".json" or ".jpg")
            ? e : ".mp3",
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
        foreach (var name in new[] { file, id + ".part", id + ".jpg" })
        {
            if (name is null) continue;
            try { System.IO.File.Delete(Path.Combine(Dir, name)); }
            catch (Exception) { /* файл играет прямо сейчас — останется до очистки */ }
        }
    }

    private void Save()
    {
        string json;
        try { json = JsonSerializer.Serialize(Items.ToList(), Json.Options); }
        catch (Exception) { return; }
        try
        {
            Directory.CreateDirectory(Dir);
            var tmp = IndexPath + ".tmp";
            System.IO.File.WriteAllText(tmp, json);
            System.IO.File.Move(tmp, IndexPath, overwrite: true);
        }
        catch (Exception) { /* запишем при следующей смене */ }
    }
}
