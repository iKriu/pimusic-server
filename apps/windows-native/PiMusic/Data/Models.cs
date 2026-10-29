using System.Text.Json;
using System.Text.Json.Serialization;

namespace PiMusic.Data;

// Модели ответов API (docs/api/README.md, bot/app/openapi.json). Имена полей — snake_case (Json.Options),
// незнакомые поля игнорируются. Ссылки stream/cover/image приходят относительными — Api дописывает сервер.

/** Строка, которую сервер может прислать и числом (id каталога, год «2016»). */
public sealed class LooseStringConverter : JsonConverter<string?>
{
    public override bool HandleNull => true;

    public override string? Read(ref Utf8JsonReader r, Type t, JsonSerializerOptions o)
    {
        switch (r.TokenType)
        {
            case JsonTokenType.String: return r.GetString() is { Length: > 0 } s ? s : null;
            case JsonTokenType.Number:
                return r.TryGetInt64(out var l) ? l.ToString() : r.GetDouble().ToString(System.Globalization.CultureInfo.InvariantCulture);
            case JsonTokenType.True: return "true";
            case JsonTokenType.False: return "false";
            default: r.Skip(); return null;
        }
    }

    public override void Write(Utf8JsonWriter w, string? v, JsonSerializerOptions o)
    {
        if (v is null) w.WriteNullValue(); else w.WriteStringValue(v);
    }
}

public static class Json
{
    public static readonly JsonSerializerOptions Options = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true,
        NumberHandling = JsonNumberHandling.AllowReadingFromString,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };
}

// ---------------------------------------------------------------- треки медиатеки / плеера

public sealed class Track
{
    public long Id { get; set; }
    public string Artist { get; set; } = "";
    public string Title { get; set; } = "";
    public string Album { get; set; } = "";
    public string Duration { get; set; } = "";
    public long Seconds { get; set; }
    public string? Cover { get; set; }
    public string? Stream { get; set; }
    public bool Owned { get; set; } = true;
    public double? Gain { get; set; }
    public double? Peak { get; set; }
    public string? Genre { get; set; }
    public int? Year { get; set; }
    public long? AddedAt { get; set; }
    public long? ItemId { get; set; }
    public string? AddedBy { get; set; }
    public string? ArtistKey { get; set; }
    public string? AlbumKey { get; set; }
    /** файл перекачивается или перекачка не удалась */
    public bool Broken { get; set; }
    public long? Size { get; set; }

    /** «Исполнитель — Альбом» для второй строки списка. */
    [JsonIgnore] public string Subtitle => string.IsNullOrEmpty(Album) ? Artist : $"{Artist} — {Album}";
    /** Абсолютные ссылки (заполняет Api при получении): обложка (null — заглушка) и поток. */
    [JsonIgnore] public Uri? CoverUri { get; set; }
    [JsonIgnore] public Uri? StreamUri { get; set; }
    /** прямой эфир («Моя Волна», радио): без длительности, перемотки и нормализации */
    [JsonIgnore] public bool Live { get; set; }
}

public sealed class TrackList { public List<Track> Items { get; set; } = []; public int Total { get; set; } }

public sealed class LibraryResult
{
    public int Total { get; set; }
    public int LibraryTotal { get; set; }
    public int Offset { get; set; }
    public int Limit { get; set; } = 50;
    public List<Track> Items { get; set; } = [];
}

public sealed class Genre { public string Name { get; set; } = ""; public int Count { get; set; } }
public sealed class Genres { public List<Genre> Items { get; set; } = []; }

public sealed class LibArtist
{
    public string Key { get; set; } = "";
    public string Name { get; set; } = "";
    public int Count { get; set; }
    public int? Year { get; set; }
    public long? AddedAt { get; set; }
    public string? Image { get; set; }
    [JsonIgnore] public Uri? ImageUri { get; set; }
    [JsonIgnore] public string CountText => Fmt.Tracks(Count);
}

public sealed class LibArtists { public List<LibArtist> Items { get; set; } = []; }

public sealed class LibAlbum
{
    public string ArtistKey { get; set; } = "";
    public string Key { get; set; } = "";
    public string Title { get; set; } = "";
    public string Artist { get; set; } = "";
    public int Count { get; set; }
    public int? Year { get; set; }
    public long? AddedAt { get; set; }
    public string? Cover { get; set; }
    [JsonIgnore] public Uri? CoverUri { get; set; }
    [JsonIgnore] public string Subtitle => Fmt.Join(Artist, Year is > 0 ? Year.ToString() : null);
}

public sealed class LibAlbums { public List<LibAlbum> Items { get; set; } = []; }

public sealed class AlbumGroup
{
    public string Key { get; set; } = "";
    public string Title { get; set; } = "";
    public string? Cover { get; set; }
    public List<Track> Tracks { get; set; } = [];
    [JsonIgnore] public Uri? CoverUri { get; set; }
}

public sealed class LibArtistPage
{
    public string Key { get; set; } = "";
    public string Name { get; set; } = "";
    public int Count { get; set; }
    public string? Image { get; set; }
    public List<AlbumGroup> Albums { get; set; } = [];
    [JsonIgnore] public Uri? ImageUri { get; set; }
}

public sealed class LibAlbumPage
{
    public string Key { get; set; } = "";
    public string Title { get; set; } = "";
    public string? Cover { get; set; }
    public List<Track> Tracks { get; set; } = [];
    public string ArtistKey { get; set; } = "";
    public string Artist { get; set; } = "";
    [JsonIgnore] public Uri? CoverUri { get; set; }
}

public sealed class LibKeys { public List<string> Keys { get; set; } = []; public List<string> Isrc { get; set; } = []; }

// ---------------------------------------------------------------- поиск и каталог

public sealed class CTrack
{
    public int I { get; set; }
    public string Artist { get; set; } = "";
    public string Title { get; set; } = "";
    public string? Album { get; set; }
    public string Duration { get; set; } = "";
    public string? Key { get; set; }
    public string? Isrc { get; set; }
    public string? Cover { get; set; }
    [JsonConverter(typeof(LooseStringConverter))] public string? Year { get; set; }
    public int? No { get; set; }
    /** уже есть в медиатеке (по /api/library/keys) */
    [JsonIgnore] public bool InLibrary { get; set; }
    [JsonIgnore] public Uri? CoverUri { get; set; }
    [JsonIgnore] public string Subtitle => Fmt.Join(Artist, Album, Year);
}

public sealed class CAlbum
{
    public int I { get; set; }
    [JsonConverter(typeof(LooseStringConverter))] public string? Id { get; set; }
    public string Title { get; set; } = "";
    [JsonConverter(typeof(LooseStringConverter))] public string? Year { get; set; }
    public string Kind { get; set; } = "album";
    public string Artist { get; set; } = "";
    [JsonConverter(typeof(LooseStringConverter))] public string? ArtistId { get; set; }
    public string? Cover { get; set; }
    [JsonIgnore] public Uri? CoverUri { get; set; }
    [JsonIgnore] public string Subtitle => Fmt.Join(Artist, Year);
}

public sealed class CArtist
{
    [JsonConverter(typeof(LooseStringConverter))] public string? Id { get; set; }
    public string Name { get; set; } = "";
    public string? Picture { get; set; }
    public long Fans { get; set; }
    [JsonIgnore] public Uri? PictureUri { get; set; }
}

public sealed class SearchResult
{
    public string Token { get; set; } = "";
    public List<CTrack> Tracks { get; set; } = [];
    public List<CAlbum> Albums { get; set; } = [];
    public List<CArtist> Artists { get; set; } = [];
}

public sealed class CatalogAlbum
{
    public string Token { get; set; } = "";
    [JsonConverter(typeof(LooseStringConverter))] public string? Id { get; set; }
    public string Title { get; set; } = "";
    public string Artist { get; set; } = "";
    [JsonConverter(typeof(LooseStringConverter))] public string? ArtistId { get; set; }
    [JsonConverter(typeof(LooseStringConverter))] public string? Year { get; set; }
    public string Kind { get; set; } = "album";
    public string? Cover { get; set; }
    public List<CTrack> Tracks { get; set; } = [];
    [JsonIgnore] public Uri? CoverUri { get; set; }
}

public sealed class CatalogArtist
{
    public string Token { get; set; } = "";
    [JsonConverter(typeof(LooseStringConverter))] public string? Id { get; set; }
    public string Name { get; set; } = "";
    public string? Picture { get; set; }
    public long Fans { get; set; }
    public List<CTrack> Tracks { get; set; } = [];
    public List<CAlbum> Albums { get; set; } = [];
    [JsonIgnore] public Uri? PictureUri { get; set; }
}

public sealed class PlRef { public long Id { get; set; } public string Name { get; set; } = ""; public int AddedNow { get; set; } }

public sealed class DownloadResp
{
    public int Queued { get; set; }
    public string Title { get; set; } = "";
    public string? Kind { get; set; }
    public string? Note { get; set; }
    public PlRef? Playlist { get; set; }
}

// ---------------------------------------------------------------- загрузки

public sealed class Job { public string Title { get; set; } = ""; public int Total { get; set; } public long Started { get; set; } }
public sealed class LogLine { public long Ts { get; set; } public string Text { get; set; } = ""; }

public sealed class ActiveItem
{
    public string Text { get; set; } = "";
    public string? Artist { get; set; }
    public string? Title { get; set; }
    public long? TrackId { get; set; }
    public string? Progress { get; set; }
}

public sealed class Activity
{
    public List<string> Active { get; set; } = [];
    public List<Job> Jobs { get; set; } = [];
    public List<LogLine> Log { get; set; } = [];
    public int Queue { get; set; }
    public List<ActiveItem> ActiveItems { get; set; } = [];
}

// ---------------------------------------------------------------- профиль, текст, вход, устройства

public sealed class Me
{
    public string Login { get; set; } = "";
    public int Tracks { get; set; }
    /** бесконечный MP3-поток «Моей Волны» (подписанная ссылка, может быть относительной) */
    public string? StreamUrl { get; set; }
    public string? JellyfinUrl { get; set; }
    public bool IsAdmin { get; set; }
}

// ---------------------------------------------------------------- плейлисты

public sealed class Member { public string Login { get; set; } = ""; }

/** Ссылка-приглашение в совместный плейлист: link — в бота (может не быть), app_link — в приложение. */
public sealed class Invite
{
    public string Token { get; set; } = "";
    public string? Link { get; set; }
    public string? AppLink { get; set; }
}

/** Плейлист: краткий вид в списке (PlaylistBrief) или целиком с tracks (GET /api/playlists/{id}). */
public sealed class Playlist
{
    public long Id { get; set; }
    public string Name { get; set; } = "";
    public string Description { get; set; } = "";
    public int Count { get; set; }
    public long Seconds { get; set; }
    public List<string> Covers { get; set; } = [];
    public bool Shared { get; set; }
    public bool InviteEnabled { get; set; }
    /** owner | member */
    public string Role { get; set; } = "owner";
    public string Owner { get; set; } = "";
    public List<Member> Members { get; set; } = [];
    public long UpdatedAt { get; set; }
    public Invite? Invite { get; set; }
    /** сортировка, с которой отдан список треков */
    public string? Sort { get; set; }
    public List<Track>? Tracks { get; set; }
    /** только в ответе на создание: сколько треков встало в очередь загрузки / добавлено сразу */
    public int Queued { get; set; }
    public int AddedNow { get; set; }

    /** Абсолютные ссылки на обложки мозаики (до 4, заполняет Api). */
    [JsonIgnore] public List<Uri> CoverUris { get; set; } = [];
    [JsonIgnore] public bool IsOwner => Role == "owner";
    [JsonIgnore] public string Subtitle => Fmt.Join(Role == "member" ? $"от {Owner}" : null, Fmt.Tracks(Count), Shared ? "общий" : null);
}

public sealed class Playlists { public List<Playlist> Items { get; set; } = []; }

/** Лист «Добавить в плейлист»: has — трек уже есть в этом плейлисте. */
public sealed class ForTrackItem { public long Id { get; set; } public string Name { get; set; } = ""; public bool Has { get; set; } }
public sealed class ForTrack { public List<ForTrackItem> Items { get; set; } = []; }

public sealed class PlAddResult { public int Added { get; set; } public int Skipped { get; set; } public int Count { get; set; } public bool Full { get; set; } }
public sealed class PlRemoveResult { public int Removed { get; set; } public int Count { get; set; } }
public sealed class PlSaveResult { public int Linked { get; set; } public int Missing { get; set; } public int Failed { get; set; } }
public sealed class PlShareResult { public bool Shared { get; set; } public Invite? Invite { get; set; } }

/** Превью приглашения (GET /api/playlists/join?token=): already — уже участник или владелец. */
public sealed class JoinPreview
{
    public long Id { get; set; }
    public string Name { get; set; } = "";
    public string Owner { get; set; } = "";
    public int Count { get; set; }
    public List<string> Covers { get; set; } = [];
    public bool Already { get; set; }
    [JsonIgnore] public List<Uri> CoverUris { get; set; } = [];
}

// ---------------------------------------------------------------- «Моя Волна» и радио

/** Что в эфире «Моей Волны». */
public sealed class WaveNow
{
    public bool Live { get; set; }
    public int Listeners { get; set; }
    public string? Artist { get; set; }
    public string? Title { get; set; }
    public string? Album { get; set; }
    public long? Duration { get; set; }
    public long? StartedAt { get; set; }
    public string? Cover { get; set; }
    public long? TrackId { get; set; }
    [JsonIgnore] public Uri? CoverUri { get; set; }
}

public sealed class WaveDiscover { public List<string> Seeds { get; set; } = []; public List<string> Queued { get; set; } = []; }
public sealed class WaveRefresh { public int Tracks { get; set; } }

/** Интернет-радиостанция: url — адрес потока (открывается напрямую плеером). */
public sealed class Station
{
    public long Id { get; set; }
    public string Name { get; set; } = "";
    public string Url { get; set; } = "";
    /** в ответе на добавление: станция ушла и в Jellyfin */
    public bool Jellyfin { get; set; }
    /** «stream.example.com» для подписи в списке */
    [JsonIgnore] public string Host => Uri.TryCreate(Url, UriKind.Absolute, out var u) ? u.Host : Url;
}

public sealed class Stations { public List<Station> Items { get; set; } = []; }

// ---------------------------------------------------------------- свои файлы и импорт списков

/**
 * Загрузка своего файла. state: receiving | queued | analyzing | need_info (спросить исполнителя и название) |
 * processing | done | duplicate | failed | cancelled.
 */
public sealed class Upload
{
    public long Id { get; set; }
    public string Name { get; set; } = "";
    public string State { get; set; } = "";
    public string StateText { get; set; } = "";
    public string? Artist { get; set; }
    public string? Title { get; set; }
    public string? Album { get; set; }
    public double? Duration { get; set; }
    public long? Size { get; set; }
    public string? Note { get; set; }
    public long CreatedAt { get; set; }
    public long UpdatedAt { get; set; }
    public Track? Track { get; set; }
    [JsonIgnore] public bool NeedInfo => State == "need_info";
    [JsonIgnore] public bool Finished => State is "done" or "duplicate" or "failed" or "cancelled";
}

public sealed class Uploads { public List<Upload> Items { get; set; } = []; }
public sealed class Rejected { public string Name { get; set; } = ""; public string Error { get; set; } = ""; public string Message { get; set; } = ""; }

/** Ответ POST /api/upload; при 413/415/429/507 часть файлов могла быть принята — тело того же вида с error/message. */
public sealed class UploadResp
{
    public List<Upload> Uploads { get; set; } = [];
    public List<Rejected> Rejected { get; set; } = [];
    public string? Error { get; set; }
    public string? Message { get; set; }
}

/** Импорт списка треков. state: running | paused | done | cancelled …; counts — счётчики по состояниям; text — сводка по-русски. */
public sealed class ImportJob
{
    public long Id { get; set; }
    public string Filename { get; set; } = "";
    public int Total { get; set; }
    public string State { get; set; } = "";
    public JsonElement? Counts { get; set; }
    public string Text { get; set; } = "";
}

public sealed class ImportList { public List<ImportJob> Imports { get; set; } = []; }

public sealed class ImportFileResp
{
    public long ImportId { get; set; }
    public string Format { get; set; } = "";
    public int Total { get; set; }
    public string Name { get; set; } = "";
    public PlRef? Playlist { get; set; }
}

public sealed class ImportFailedItem
{
    public string Artist { get; set; } = "";
    public string Title { get; set; } = "";
    public string Album { get; set; } = "";
    public string Note { get; set; } = "";
}

public sealed class ImportFailed { public List<ImportFailedItem> Items { get; set; } = []; }

// ---------------------------------------------------------------- настройки между устройствами

/** Значение настройки и время изменения (миллисекунды) — base для следующей записи. */
public sealed class SettingItem { public JsonElement? Value { get; set; } public long UpdatedAt { get; set; } }
public sealed class SettingsAll { public Dictionary<string, SettingItem> Items { get; set; } = []; }
public sealed class SettingPut { public string Key { get; set; } = ""; public JsonElement? Value { get; set; } public long UpdatedAt { get; set; } }

public sealed class LyricLine { public double T { get; set; } public string Text { get; set; } = ""; }
public sealed class Lyrics { public List<LyricLine>? Synced { get; set; } public string? Plain { get; set; } public string? Source { get; set; } }

public sealed class AuthStart
{
    public string Nonce { get; set; } = "";
    public string? Code { get; set; }
    public string? Link { get; set; }
    public int ExpiresIn { get; set; } = 300;
}

/** status: pending | ok | denied | expired | too_many_devices. */
public sealed class AuthPoll
{
    public string Status { get; set; } = "";
    public string? Token { get; set; }
    public string? Login { get; set; }
    public string? Message { get; set; }
}

public sealed class Device
{
    public long Id { get; set; }
    public string Name { get; set; } = "";
    public string Platform { get; set; } = "";
    public long CreatedAt { get; set; }
    public long LastSeen { get; set; }
    public bool Current { get; set; }
    [JsonIgnore] public bool CanRevoke => !Current;
    [JsonIgnore] public string Details => Current ? $"{Platform} · это устройство" : $"{Platform} · был в сети {Fmt.Date(LastSeen)}";
}

public sealed class Devices { public List<Device> Items { get; set; } = []; }

public sealed class ServerInfo
{
    public int ApiVersion { get; set; }
    public List<string> Features { get; set; } = [];
}

public sealed class LibDeleteResp
{
    public int Deleted { get; set; }
    public List<long> DeletedIds { get; set; } = [];
    public List<long> GoneIds { get; set; } = [];
}

// ---------------------------------------------------------------- форматирование для интерфейса

public static class Fmt
{
    private static readonly System.Globalization.CultureInfo Ru = new("ru-RU");

    public static string Tracks(int n)
    {
        var m10 = n % 10; var m100 = n % 100;
        var w = m10 == 1 && m100 != 11 ? "трек" : m10 is >= 2 and <= 4 && (m100 < 12 || m100 > 14) ? "трека" : "треков";
        return $"{n} {w}";
    }

    public static string Join(params string?[] parts) => string.Join(" · ", parts.Where(p => !string.IsNullOrEmpty(p)));

    /** Размер файла: «850 КБ», «12,4 МБ», «1,25 ГБ». */
    public static string Size(long bytes) => bytes switch
    {
        < 1024 * 1024 => $"{(Math.Max(0, bytes) + 1023) / 1024} КБ",
        < 1024L * 1024 * 1024 => (bytes / 1048576.0).ToString("0.#", Ru) + " МБ",
        _ => (bytes / 1073741824.0).ToString("0.##", Ru) + " ГБ",
    };

    public static string Time(TimeSpan t) => t.TotalHours >= 1 ? t.ToString(@"h\:mm\:ss") : t.ToString(@"m\:ss");

    public static string Date(long unix) =>
        unix <= 0 ? "—" : DateTimeOffset.FromUnixTimeSeconds(unix).ToLocalTime().ToString("d MMMM yyyy", Ru);
}
