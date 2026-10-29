using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;

namespace PiMusic.Data;

/**
 * Ошибка API: Status 0 — нет связи; Code — поле error ответа; Message — текст для пользователя;
 * Body — всё тело ошибки (например, current при 409 conflict настроек).
 */
public sealed class ApiException(int status, string code, string message, JsonElement? body = null) : Exception(message)
{
    public int Status { get; } = status;
    public string Code { get; } = code;
    public JsonElement? Body { get; } = body;
}

/**
 * JSON API PiMusic (docs/api/README.md) с `Authorization: Bearer <токен устройства>`. 401 — токен стёрт и
 * событие AuthLost (экран входа). Относительные ссылки stream/cover/image в ответах дополняются адресом сервера.
 */
public sealed class Api
{
    // Версия — раньше Instance: статические поля инициализируются по порядку, а конструктор её использует.
    public static string Version { get; } = typeof(Api).Assembly.GetName().Version?.ToString(3) ?? "1.1.0";
    public static Api Instance { get; } = new();

    private static readonly Dictionary<int, string> Messages = new()
    {
        [0] = "Нет связи с сервером",
        [401] = "Вход больше не действует — войдите снова",
        [403] = "Нет доступа",
        [404] = "Не найдено",
        [409] = "Уже выполняется",
        [429] = "Слишком часто — попробуйте чуть позже",
        [500] = "Ошибка сервера",
        [502] = "Внешний сервис недоступен",
        [504] = "Внешний сервис не ответил",
    };

    private readonly HttpClient _http;
    private readonly AppSettings _s = AppSettings.Current;

    /** Вход больше не действует (401): показать экран входа. */
    public event Action? AuthLost;


    private Api()
    {
        _http = new HttpClient(new SocketsHttpHandler
        {
            AllowAutoRedirect = false,
            ConnectTimeout = TimeSpan.FromSeconds(15),
            AutomaticDecompression = DecompressionMethods.All,
        }) { Timeout = TimeSpan.FromSeconds(60) };
        _http.DefaultRequestHeaders.UserAgent.ParseAdd($"PiMusic-Windows/{Version}");
    }

    public string Server => _s.Server.TrimEnd('/');
    public bool LoggedIn => _s.Token is not null;

    /** Абсолютный адрес для ссылки из ответа (относительной или уже полной). */
    public Uri? Abs(string? path)
    {
        if (string.IsNullOrEmpty(path)) return null;
        if (Server.Length == 0 && !path.Contains("://")) return null; // сервер ещё не выбран
        // полный адрес — только http(s); относительный — от адреса сервера (битый адрес — без ссылки)
        if (!path.StartsWith('/') && Uri.TryCreate(path, UriKind.Absolute, out var u))
            return u.Scheme == Uri.UriSchemeHttps || u.Scheme == Uri.UriSchemeHttp ? u : null;
        return Uri.TryCreate(Server + path, UriKind.Absolute, out var r) ? r : null;
    }

    /** Адрес запроса API; адрес сервера пустой или битый — ApiException, а не UriFormatException мимо обработчиков. */
    private Uri ApiUri(string path)
    {
        if (Uri.TryCreate(Server + "/api" + path, UriKind.Absolute, out var u)
            && (u.Scheme == Uri.UriSchemeHttps || u.Scheme == Uri.UriSchemeHttp))
            return u;
        throw new ApiException(0, "bad_server", Server.Length == 0 ? "Не указан адрес сервера" : "Неверный адрес сервера");
    }

    // ---------------------------------------------------------------- транспорт

    private Task<T> Send<T>(HttpMethod method, string path, object? body = null, bool auth = true, CancellationToken ct = default) =>
        Exec<T>(_http, method, path, body is null ? null : JsonContent.Create(body, body.GetType(), options: Json.Options), auth, null, ct);

    /** partialOk — статусы, при которых тело ошибки того же вида, что и успешный ответ (частично принятая загрузка). */
    private async Task<T> Exec<T>(HttpClient http, HttpMethod method, string path, HttpContent? content, bool auth,
        int[]? partialOk, CancellationToken ct)
    {
        using var req = new HttpRequestMessage(method, ApiUri(path));
        if (auth && _s.Token is { } token) req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        req.Content = content;
        HttpResponseMessage resp;
        try { resp = await http.SendAsync(req, ct); }
        catch (OperationCanceledException) when (ct.IsCancellationRequested) { throw; }
        catch (Exception) { throw new ApiException(0, "network", Messages[0]); }

        using (resp)
        {
            var status = (int)resp.StatusCode;
            var text = await resp.Content.ReadAsStringAsync(ct);
            if (resp.IsSuccessStatusCode || (partialOk?.Contains(status) == true && text.TrimStart().StartsWith('{')))
            {
                if (typeof(T) == typeof(JsonElement) && string.IsNullOrWhiteSpace(text)) return default!;
                try { return JsonSerializer.Deserialize<T>(text, Json.Options)!; }
                catch (JsonException) when (!resp.IsSuccessStatusCode) { }
                catch (JsonException) { throw new ApiException(status, "bad_response", "Сервер прислал непонятный ответ"); }
            }
            string code = "", message = "";
            JsonElement? data = null;
            try
            {
                using var doc = JsonDocument.Parse(text);
                data = doc.RootElement.Clone();
                if (doc.RootElement.TryGetProperty("error", out var e)) code = e.GetString() ?? "";
                if (doc.RootElement.TryGetProperty("message", out var m)) message = m.GetString() ?? "";
            }
            catch (JsonException) { }
            catch (InvalidOperationException) { /* error не строка */ }
            if (status == 401 && auth)
            {
                _s.Token = null;
                _s.Save();
                AuthLost?.Invoke();
            }
            if (string.IsNullOrEmpty(message))
                message = code == "not_registered" ? "Аккаунт Telegram не зарегистрирован в боте"
                    : Messages.GetValueOrDefault(status) ?? $"Ошибка {status}";
            throw new ApiException(status, code, message, data);
        }
    }

    private Task<T> Get<T>(string path, CancellationToken ct = default) => Send<T>(HttpMethod.Get, path, ct: ct);
    private Task<T> Post<T>(string path, object? body = null, CancellationToken ct = default) => Send<T>(HttpMethod.Post, path, body ?? new { }, ct: ct);

    /** Клиент для долгих отправок файлов: без общего тайм-аута (отмена — через CancellationToken). */
    private HttpClient UploadClient => _upload ??= MakeUploadClient();
    private HttpClient? _upload;

    private static HttpClient MakeUploadClient()
    {
        var c = new HttpClient(new SocketsHttpHandler { AllowAutoRedirect = false, ConnectTimeout = TimeSpan.FromSeconds(15) })
            { Timeout = Timeout.InfiniteTimeSpan };
        c.DefaultRequestHeaders.UserAgent.ParseAdd($"PiMusic-Windows/{Version}");
        return c;
    }

    /** multipart/form-data из файлов (поле field у каждого); progress — (отправлено байт, всего). */
    private Task<T> Multipart<T>(string path, IReadOnlyList<string> files, string field, IProgress<(long Sent, long Total)>? progress,
        int[]? partialOk, CancellationToken ct)
    {
        var total = files.Sum(f => new FileInfo(f).Length);
        var sent = new long[1];
        var form = new MultipartFormDataContent();
        foreach (var f in files)
        {
            var part = new ProgressFileContent(f, n => progress?.Report((Interlocked.Add(ref sent[0], n), total)));
            part.Headers.ContentType = new MediaTypeHeaderValue(MimeOf(f));
            form.Add(part, field, Path.GetFileName(f));
        }
        return Exec<T>(UploadClient, HttpMethod.Post, path, form, true, partialOk, ct);
    }

    private static string MimeOf(string file) => Path.GetExtension(file).ToLowerInvariant() switch
    {
        ".mp3" => "audio/mpeg",
        ".flac" => "audio/flac",
        ".m4a" or ".mp4" or ".aac" => "audio/mp4",
        ".ogg" or ".oga" or ".opus" => "audio/ogg",
        ".wav" => "audio/wav",
        ".txt" => "text/plain",
        ".csv" => "text/csv",
        ".tsv" => "text/tab-separated-values",
        _ => "application/octet-stream",
    };

    /** Содержимое-файл, сообщающее, сколько байт ушло. */
    private sealed class ProgressFileContent(string file, Action<long> sent) : HttpContent
    {
        protected override async Task SerializeToStreamAsync(Stream stream, System.Net.TransportContext? context)
        {
            await using var src = File.OpenRead(file);
            var buf = new byte[64 * 1024];
            int n;
            while ((n = await src.ReadAsync(buf)) > 0)
            {
                await stream.WriteAsync(buf.AsMemory(0, n));
                sent(n);
            }
        }

        protected override bool TryComputeLength(out long length)
        {
            length = new FileInfo(file).Length;
            return true;
        }
    }

    private static string Q(params (string k, object? v)[] ps)
    {
        var parts = ps.Where(p => p.v is not null && p.v.ToString() != "")
            .Select(p => $"{p.k}={Uri.EscapeDataString(Convert.ToString(p.v, System.Globalization.CultureInfo.InvariantCulture)!)}");
        var s = string.Join("&", parts);
        return s.Length == 0 ? "" : "?" + s;
    }

    // ---------------------------------------------------------------- ссылки в ответах

    private T Fix<T>(T x)
    {
        switch (x)
        {
            case Track t: t.CoverUri = Abs(t.Cover); t.StreamUri = Abs(t.Stream); break;
            case IEnumerable<Track> ts: foreach (var t in ts) Fix(t); break;
            case LibraryResult p: Fix(p.Items); break;
            case TrackList l: Fix(l.Items); break;
            case LibArtists a: foreach (var i in a.Items) i.ImageUri = Abs(i.Image); break;
            case LibAlbums a: foreach (var i in a.Items) i.CoverUri = Abs(i.Cover); break;
            case LibArtistPage p:
                p.ImageUri = Abs(p.Image);
                foreach (var g in p.Albums) { g.CoverUri = Abs(g.Cover); Fix(g.Tracks); }
                break;
            case LibAlbumPage p: p.CoverUri = Abs(p.Cover); Fix(p.Tracks); break;
            case SearchResult r:
                foreach (var t in r.Tracks) t.CoverUri = Abs(t.Cover);
                foreach (var a in r.Albums) a.CoverUri = Abs(a.Cover);
                foreach (var a in r.Artists) a.PictureUri = Abs(a.Picture);
                break;
            case CatalogAlbum a:
                a.CoverUri = Abs(a.Cover);
                foreach (var t in a.Tracks) t.CoverUri = Abs(t.Cover ?? a.Cover);
                break;
            case CatalogArtist a:
                a.PictureUri = Abs(a.Picture);
                foreach (var t in a.Tracks) t.CoverUri = Abs(t.Cover);
                foreach (var al in a.Albums) al.CoverUri = Abs(al.Cover);
                break;
            case Playlist p:
                p.CoverUris = [.. p.Covers.Select(Abs).OfType<Uri>()];
                if (p.Tracks is not null) Fix(p.Tracks);
                break;
            case Playlists l: foreach (var p in l.Items) Fix(p); break;
            case JoinPreview j: j.CoverUris = [.. j.Covers.Select(Abs).OfType<Uri>()]; break;
            case WaveNow w: w.CoverUri = Abs(w.Cover); break;
            case Upload u: if (u.Track is not null) Fix(u.Track); break;
            case Uploads l: foreach (var u in l.Items) Fix(u); break;
        }
        return x;
    }

    // ---------------------------------------------------------------- вход и аккаунт

    public Task<ServerInfo> Info(CancellationToken ct = default) => Send<ServerInfo>(HttpMethod.Get, "/info", auth: false, ct: ct);

    public Task<AuthStart> AuthStart(string device) =>
        Send<AuthStart>(HttpMethod.Post, "/auth/start", new { device, platform = "windows" }, auth: false);

    public Task<AuthPoll> AuthPoll(string nonce, CancellationToken ct = default) =>
        Send<AuthPoll>(HttpMethod.Post, "/auth/poll", new { nonce }, auth: false, ct: ct);

    public async Task Logout(bool all = false)
    {
        try { await Post<JsonElement>("/auth/logout", new { all }); }
        finally { _s.Token = null; _s.Login = null; _s.Save(); }
    }

    public Task<Me> Me() => Get<Me>("/me");
    public Task<Devices> Devices() => Get<Devices>("/devices");
    public Task<JsonElement> RevokeDevice(long id) => Post<JsonElement>($"/devices/{id}/revoke");

    // ---------------------------------------------------------------- медиатека

    public async Task<LibraryResult> Library(string? q = null, string? genre = null, string sort = "added", string? order = null,
        int offset = 0, int limit = 50, long? seed = null, CancellationToken ct = default) =>
        Fix(await Get<LibraryResult>("/library" + Q(("q", q), ("genre", genre), ("sort", sort), ("order", order),
            ("offset", offset), ("limit", limit), ("seed", seed)), ct));

    /** Вся текущая выборка для «Слушать»/«Перемешать» (до 3000 треков). */
    public async Task<TrackList> LibraryPlay(string? q = null, string? genre = null, string sort = "added", string? order = null, long? seed = null) =>
        Fix(await Get<TrackList>("/library/play" + Q(("q", q), ("genre", genre), ("sort", sort), ("order", order), ("seed", seed))));

    public async Task<LibArtists> Artists(string? genre = null, string sort = "name") =>
        Fix(await Get<LibArtists>("/library/artists" + Q(("genre", genre), ("sort", sort))));

    public async Task<LibAlbums> Albums(string? genre = null, string sort = "added") =>
        Fix(await Get<LibAlbums>("/library/albums" + Q(("genre", genre), ("sort", sort))));

    public Task<Genres> Genres() => Get<Genres>("/library/genres");

    public async Task<LibArtistPage> Artist(string key) => Fix(await Get<LibArtistPage>("/library/artist" + Q(("a", key))));

    public async Task<LibAlbumPage> Album(string artistKey, string albumKey) =>
        Fix(await Get<LibAlbumPage>("/library/album" + Q(("a", artistKey), ("b", albumKey))));

    /** Свежие ссылки stream/cover (до 500 id, порядок сохраняется, недоступные пропущены). */
    public async Task<TrackList> Tracks(IEnumerable<long> ids) =>
        Fix(await Get<TrackList>("/tracks" + Q(("ids", string.Join(",", ids)))));

    public Task<LibKeys> LibraryKeys() => Get<LibKeys>("/library/keys");

    public Task<LibDeleteResp> Delete(IEnumerable<long> ids) => Post<LibDeleteResp>("/library/delete", new { track_ids = ids.ToArray() });

    public Task<Lyrics> Lyrics(long id) => Get<Lyrics>($"/lyrics/{id}");

    // ---------------------------------------------------------------- поиск, каталог, загрузки

    public async Task<SearchResult> Search(string q, CancellationToken ct = default) => Fix(await Get<SearchResult>("/search" + Q(("q", q)), ct));
    public async Task<CatalogAlbum> CatalogAlbum(string id) => Fix(await Get<CatalogAlbum>("/catalog/album" + Q(("id", id))));
    public async Task<CatalogArtist> CatalogArtist(string id) => Fix(await Get<CatalogArtist>("/catalog/artist" + Q(("id", id))));

    /** kind: track (i) | all | album (i) | tracks (idx) | album_track (i, j); playlistId — заодно поставить в плейлист. */
    public Task<DownloadResp> Download(string token, string kind, int? i = null, int? j = null, int[]? idx = null, long? playlistId = null) =>
        Post<DownloadResp>("/download", new { token, kind, i, j, idx, playlist_id = playlistId });

    public Task<DownloadResp> DownloadLink(string url) => Post<DownloadResp>("/download/link", new { url });
    public Task<Activity> Activity(CancellationToken ct = default) => Get<Activity>("/activity", ct);

    // ---------------------------------------------------------------- плейлисты

    public async Task<Playlists> Playlists(CancellationToken ct = default) => Fix(await Get<Playlists>("/playlists", ct));

    /** sort: manual | added | title | artist | album | duration | random (с seed); order: asc | desc. */
    public async Task<Playlist> Playlist(long id, string sort = "manual", string? order = null, long? seed = null, CancellationToken ct = default) =>
        Fix(await Get<Playlist>($"/playlists/{id}" + Q(("sort", sort), ("order", order), ("seed", seed)), ct));

    /**
     * Создать: trackIds — сразу добавить треки медиатеки; catalog — тело как у /api/download
     * (new { token, kind, i, j, idx }): плейлист наполнится по мере загрузки.
     */
    public async Task<Playlist> CreatePlaylist(string name, string? description = null, bool shared = false,
        IEnumerable<long>? trackIds = null, object? catalog = null) =>
        Fix(await Post<Playlist>("/playlists", new { name, description, shared, track_ids = trackIds?.ToArray(), catalog }));

    public async Task<Playlist> UpdatePlaylist(long id, string? name = null, string? description = null) =>
        Fix(await Post<Playlist>($"/playlists/{id}/update", new { name, description }));

    /** Ссылка-приглашение: enabled — включить/выключить; regenerate — новая ссылка (старая перестанет работать). */
    public Task<PlShareResult> SharePlaylist(long id, bool enabled, bool regenerate = false) =>
        Post<PlShareResult>($"/playlists/{id}/share", new { enabled, regenerate });

    public Task<PlAddResult> PlaylistAdd(long id, IEnumerable<long> trackIds) =>
        Post<PlAddResult>($"/playlists/{id}/add", new { track_ids = trackIds.ToArray() });

    /** Добавить альбом медиатеки (artist_key, album_key) или всего исполнителя (artist_key). */
    public Task<PlAddResult> PlaylistAddAlbum(long id, string artistKey, string albumKey) =>
        Post<PlAddResult>($"/playlists/{id}/add", new { album = new { a = artistKey, b = albumKey } });

    public Task<PlAddResult> PlaylistAddArtist(long id, string artistKey) =>
        Post<PlAddResult>($"/playlists/{id}/add", new { artist = artistKey });

    /** itemIds — Track.ItemId (позиции в плейлисте), не id треков. */
    public Task<PlRemoveResult> PlaylistRemove(long id, IEnumerable<long> itemIds) =>
        Post<PlRemoveResult>($"/playlists/{id}/remove", new { item_ids = itemIds.ToArray() });

    /** Перестановка (только при sort=manual): to — новая позиция с 0. */
    public Task<JsonElement> PlaylistMove(long id, long itemId, int to) =>
        Post<JsonElement>($"/playlists/{id}/move", new { item_id = itemId, to });

    public Task<JsonElement> PlaylistMoveMany(long id, IEnumerable<long> itemIds, int to) =>
        Post<JsonElement>($"/playlists/{id}/move-many", new { item_ids = itemIds.ToArray(), to });

    public Task<JsonElement> DeletePlaylist(long id) => Post<JsonElement>($"/playlists/{id}/delete");
    public Task<JsonElement> LeavePlaylist(long id) => Post<JsonElement>($"/playlists/{id}/leave");
    public async Task<Playlist> KickMember(long id, string login) => Fix(await Post<Playlist>($"/playlists/{id}/kick", new { login }));

    /** «Сохранить в медиатеку»: недостающие треки — в свою медиатеку без скачивания. */
    public Task<PlSaveResult> SavePlaylist(long id) => Post<PlSaveResult>($"/playlists/{id}/save");

    public async Task<JoinPreview> JoinPreview(string token) => Fix(await Get<JoinPreview>("/playlists/join" + Q(("token", token))));
    public async Task<Playlist> JoinPlaylist(string token) => Fix(await Post<Playlist>("/playlists/join", new { token }));

    public Task<ForTrack> PlaylistsForTrack(long trackId) => Get<ForTrack>("/playlists/for-track" + Q(("track_id", trackId)));

    // ---------------------------------------------------------------- «Моя Волна» и радио

    public async Task<WaveNow> WaveNow(CancellationToken ct = default) => Fix(await Get<WaveNow>("/wave/now", ct));
    public Task<WaveDiscover> WaveDiscover() => Post<WaveDiscover>("/wave/discover");
    public Task<WaveRefresh> WaveRefresh() => Post<WaveRefresh>("/wave/refresh");

    public Task<Stations> Radio() => Get<Stations>("/radio");
    public Task<Station> AddRadio(string name, string url) => Post<Station>("/radio", new { name, url });
    /** Только администратор. */
    public Task<JsonElement> DeleteRadio(long id) => Post<JsonElement>($"/radio/{id}/delete");

    // ---------------------------------------------------------------- свои файлы и импорт списков

    private static readonly int[] UploadPartial = [413, 415, 429, 507];

    /** Отправить аудиофайлы (до 50 за раз). Отклонённые — в Rejected; при 413/415/429/507 часть могла быть принята. */
    public async Task<UploadResp> Upload(IReadOnlyList<string> files, IProgress<(long Sent, long Total)>? progress = null, CancellationToken ct = default)
    {
        var r = await Multipart<UploadResp>("/upload", files, "file", progress, UploadPartial, ct);
        foreach (var u in r.Uploads) Fix(u);
        return r;
    }

    public async Task<Uploads> Uploads(CancellationToken ct = default) => Fix(await Get<Uploads>("/uploads", ct));

    /** Ответ на need_info: исполнитель и название обязательны. */
    public async Task<Upload> UploadInfo(long id, string artist, string title, string? album) =>
        Fix(await Post<Upload>($"/uploads/{id}/info", new { artist, title, album }));

    /** Оставить как есть (без тегов). */
    public async Task<Upload> UploadSkip(long id) => Fix(await Post<Upload>($"/uploads/{id}/skip"));
    public async Task<Upload> UploadCancel(long id) => Fix(await Post<Upload>($"/uploads/{id}/cancel"));

    public Task<ImportList> Imports(CancellationToken ct = default) => Get<ImportList>("/import", ct);

    /** Список треков TXT/CSV/TSV (до 2 МБ). */
    public Task<ImportFileResp> ImportFile(string file, CancellationToken ct = default) =>
        Multipart<ImportFileResp>("/import/file", [file], "file", null, null, ct);

    public Task<JsonElement> ImportPause(long id) => Post<JsonElement>("/import/pause", new { id });
    public Task<JsonElement> ImportResume(long id) => Post<JsonElement>("/import/resume", new { id });
    public Task<JsonElement> ImportCancel(long id) => Post<JsonElement>("/import/cancel", new { id });
    public Task<ImportFailed> ImportFailed(long id) => Get<ImportFailed>("/import/failed" + Q(("id", id)));

    // ---------------------------------------------------------------- настройки между устройствами

    public Task<SettingsAll> Settings(CancellationToken ct = default) => Get<SettingsAll>("/settings", ct);

    /**
     * Записать ключ: baseUpdatedAt — прежний updated_at (null — без проверки). Изменили на другом устройстве —
     * ApiException 409 conflict, в Body.current — текущее значение.
     */
    public Task<SettingPut> PutSetting(string key, JsonElement value, long? baseUpdatedAt) =>
        Send<SettingPut>(HttpMethod.Put, "/settings/" + Uri.EscapeDataString(key), new { value, @base = baseUpdatedAt });

    public Task<JsonElement> DeleteSetting(string key) => Send<JsonElement>(HttpMethod.Delete, "/settings/" + Uri.EscapeDataString(key));
}
