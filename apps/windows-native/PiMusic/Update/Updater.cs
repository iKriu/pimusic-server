using System.Diagnostics;
using System.Net;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Org.BouncyCastle.Math.EC.Rfc8032;
using PiMusic.Data;

namespace PiMusic.Update;

/** Найденное обновление: данные из windows-native.json. */
public sealed record UpdateInfo(string Version, string Notes, string Url, string Sha256, long Size);

/**
 * Автообновление (docs/design/APPS_API.md, «Обновления»): windows-native.json из последнего выпуска репозитория на
 * GitHub (подпись ed25519 — windows-native.json.sig там же) → загрузка установщика Inno Setup, сверка sha256 и
 * размера, тихий запуск установщика и выход из приложения. Только https; перенаправления — только на хосты
 * загрузок GitHub (AllowedHosts), не больше MaxRedirects.
 */
public sealed partial class Updater
{
    private const string Repo = "iKriu/pimusic-server";
    private const string ManifestUrl = "https://github.com/" + Repo + "/releases/latest/download/windows-native.json";
    private static readonly string[] AllowedHosts = ["github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"];
    private const int MaxRedirects = 5;
    private const string InstallerPath = "/" + Repo + "/releases/download/";
    /** больше манифест и подпись не бывают: защита от бесконечного ответа */
    private const int MaxManifestBytes = 64 * 1024;
    private const long CheckIntervalSeconds = 24 * 3600;

    /**
     * Публичный ключ подписи манифеста: ed25519, 32 байта в base64 (44 символа). Своя сборка из форка — свой ключ:
     * `openssl genpkey -algorithm ed25519` (закрытый ключ — только у вас), публичный — сюда и Repo — на свой
     * репозиторий (apps/build/release-manifests.sh). Пустая строка — обновления отключены (без подписи не ставим).
     * Обновление предлагается только при верной подписи
     * windows-native.json.sig (base64 от 64 байт подписи над точными байтами манифеста).
     */
    private const string ManifestPublicKey = "6+nytMs179e2js+DDvQ/DERlqGHRUwEEENOaE9xHYUw=";

    /** Версия сборки, напр. "1.1.0". */
    public static string CurrentVersion { get; } = ReadCurrentVersion();

    // Instance — после CurrentVersion: статические поля инициализируются по порядку, конструктор её использует.
    public static Updater Instance { get; } = new();

    private readonly HttpClient _http;

    private Updater()
    {
        var handler = new SocketsHttpHandler { AllowAutoRedirect = false, ConnectTimeout = TimeSpan.FromSeconds(15) };
        _http = new HttpClient(handler) { Timeout = TimeSpan.FromMinutes(30) };
        _http.DefaultRequestHeaders.UserAgent.ParseAdd("PiMusic-Windows/" + CurrentVersion);
    }

    private static string ReadCurrentVersion()
    {
        var v = Assembly.GetExecutingAssembly().GetName().Version;
        return v is null ? "1.1.0" : $"{v.Major}.{v.Minor}.{Math.Max(v.Build, 0)}";
    }

    /**
     * manual=false: не чаще раза в сутки и без пропущенной версии, ошибки сети глотаются (null);
     * manual=true — всегда, ошибка — исключение с русским текстом. null — обновлений нет.
     */
    public async Task<UpdateInfo?> CheckAsync(bool manual)
    {
        if (ManifestPublicKey.Length == 0)
        {
            if (manual) throw new InvalidOperationException("Обновления отключены: в этой сборке нет ключа подписи");
            return null;
        }
        var s = AppSettings.Current;
        var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        if (!manual && now - s.LastUpdateCheck < CheckIntervalSeconds) return null;
        try
        {
            var bytes = await GetManifestBytesAsync().ConfigureAwait(false);
            if (!await VerifyAsync(bytes, ManifestPublicKey).ConfigureAwait(false))
            {
                // Публикация могла попасть между чтением манифеста и подписи: один повтор с чистого листа.
                await Task.Delay(TimeSpan.FromSeconds(2)).ConfigureAwait(false);
                bytes = await GetManifestBytesAsync().ConfigureAwait(false);
                if (!await VerifyAsync(bytes, ManifestPublicKey).ConfigureAwait(false))
                    throw new InvalidOperationException("Подпись манифеста обновлений неверна");
            }
            var info = Parse(Encoding.UTF8.GetString(bytes));
            s.LastUpdateCheck = now;
            s.Save();
            if (!IsNewer(info.Version, CurrentVersion)) return null;
            if (!manual && info.Version == s.SkippedVersion) return null;
            return info;
        }
        catch (Exception e)
        {
            if (!manual) return null;
            if (e is InvalidOperationException) throw;
            throw new InvalidOperationException(e is HttpRequestException or TaskCanceledException
                ? "Нет связи с сервером обновлений" : "Не удалось проверить обновления: " + e.Message, e);
        }
    }

    /** Свежий манифест байтами (в обход кэша). */
    private async Task<byte[]> GetManifestBytesAsync()
    {
        using var resp = await GetAsync(Fresh(ManifestUrl), "манифеста", HttpCompletionOption.ResponseHeadersRead, default).ConfigureAwait(false);
        if (resp.StatusCode != HttpStatusCode.OK) throw new InvalidOperationException($"Сервер ответил {(int)resp.StatusCode}");
        return await ReadLimitedAsync(resp).ConfigureAwait(false);
    }

    /** Тело небольшого ответа (манифест, подпись) не больше MaxManifestBytes. */
    private static async Task<byte[]> ReadLimitedAsync(HttpResponseMessage resp)
    {
        if (resp.Content.Headers.ContentLength > MaxManifestBytes) throw new InvalidOperationException("Манифест обновлений слишком большой");
        await using var src = await resp.Content.ReadAsStreamAsync().ConfigureAwait(false);
        using var ms = new MemoryStream();
        var buf = new byte[8192];
        int n;
        while ((n = await src.ReadAsync(buf).ConfigureAwait(false)) > 0)
        {
            if (ms.Length + n > MaxManifestBytes) throw new InvalidOperationException("Манифест обновлений слишком большой");
            ms.Write(buf, 0, n);
        }
        return ms.ToArray();
    }

    /** GET с ручными перенаправлениями: каждый шаг — https и хост из AllowedHosts. */
    private async Task<HttpResponseMessage> GetAsync(string url, string what, HttpCompletionOption mode, CancellationToken ct)
    {
        var uri = new Uri(url);
        for (var hop = 0; ; hop++)
        {
            RequireOwnHost(uri.AbsoluteUri, what);
            var resp = await _http.GetAsync(uri, mode, ct).ConfigureAwait(false);
            if ((int)resp.StatusCode is not (>= 300 and < 400)) return resp;
            var next = resp.Headers.Location;
            resp.Dispose();
            if (next is null || hop >= MaxRedirects) throw new InvalidOperationException("Слишком много перенаправлений при загрузке обновления");
            uri = next.IsAbsoluteUri ? next : new Uri(uri, next);
        }
    }

    /** Скачать windows-native.json.sig и проверить подпись над байтами манифеста; нет файла / не base64 — false. */
    private async Task<bool> VerifyAsync(byte[] manifest, string publicKey)
    {
        using var resp = await GetAsync(Fresh(ManifestUrl + ".sig"), "подписи", HttpCompletionOption.ResponseHeadersRead, default).ConfigureAwait(false);
        if (resp.StatusCode != HttpStatusCode.OK) return false;
        var sig = Encoding.UTF8.GetString(await ReadLimitedAsync(resp).ConfigureAwait(false));
        return ManifestSignature.Verify(manifest, sig, publicKey);
    }

    /** Адрес с меткой времени: прокси/браузер не отдадут устаревший файл (GitHub параметр игнорирует). */
    private static string Fresh(string url) => url + "?t=" + DateTimeOffset.UtcNow.ToUnixTimeSeconds();

    /** Скачать установщик, сверить sha256 и размер, запустить тихую установку и закрыть приложение. */
    public async Task InstallAsync(UpdateInfo info, IProgress<double> progress, CancellationToken ct)
    {
        if (ManifestPublicKey.Length == 0) throw new InvalidOperationException("Обновления отключены: в этой сборке нет ключа подписи");
        RequireInstallerUrl(info.Url);
        var name = Path.GetFileName(new Uri(info.Url).AbsolutePath);
        if (!SafeName().IsMatch(name) || !name.EndsWith(".exe", StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("Недопустимое имя файла установщика");
        var dir = Path.Combine(Path.GetTempPath(), "PiMusic-update");
        Directory.CreateDirectory(dir);
        foreach (var old in Directory.EnumerateFiles(dir)) TryDelete(old);
        var file = Path.Combine(dir, name);
        try
        {
            using var resp = await GetAsync(info.Url, "установщика", HttpCompletionOption.ResponseHeadersRead, ct);
            if (resp.StatusCode != HttpStatusCode.OK) throw new InvalidOperationException($"Сервер ответил {(int)resp.StatusCode}");
            var total = resp.Content.Headers.ContentLength ?? info.Size;
            using var sha = SHA256.Create();
            long done = 0;
            await using (var src = await resp.Content.ReadAsStreamAsync(ct))
            await using (var dst = new FileStream(file, FileMode.Create, FileAccess.Write, FileShare.None, 81920, true))
            {
                var buf = new byte[81920];
                int n;
                while ((n = await src.ReadAsync(buf, ct)) > 0)
                {
                    await dst.WriteAsync(buf.AsMemory(0, n), ct);
                    sha.TransformBlock(buf, 0, n, null, 0);
                    done += n;
                    if (info.Size > 0 && done > info.Size) throw new InvalidOperationException("Размер файла не совпал с манифестом");
                    if (total > 0) progress.Report(Math.Min(1.0, (double)done / total));
                }
            }
            sha.TransformFinalBlock([], 0, 0);
            if (info.Size > 0 && done != info.Size) throw new InvalidOperationException("Размер файла не совпал с манифестом");
            if (!string.Equals(Convert.ToHexString(sha.Hash!), info.Sha256, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("Контрольная сумма установщика не совпала");
            progress.Report(1.0);
        }
        catch (HttpRequestException)
        {
            TryDelete(file);
            throw new InvalidOperationException("Нет связи с сервером обновлений");
        }
        catch (Exception)
        {
            TryDelete(file);
            throw;
        }

        // установщик сам закрывает PiMusic (CloseApplications) и после тихой установки запускает его снова ([Run] в PiMusic.iss)
        if (Process.Start(new ProcessStartInfo(file, "/SILENT /SUPPRESSMSGBOXES /NORESTART") { UseShellExecute = false }) is null)
            throw new InvalidOperationException("Не удалось запустить установщик");
        // Выход через трей: сохранить очередь, убрать значок, закрыть приложение.
        PiMusic.Services.TrayService.ExitApp();
    }

    /** Больше не предлагать эту версию. */
    public void Skip(UpdateInfo info)
    {
        AppSettings.Current.SkippedVersion = info.Version;
        AppSettings.Current.Save();
    }

    private static UpdateInfo Parse(string text)
    {
        try
        {
            using var doc = JsonDocument.Parse(text);
            var r = doc.RootElement;
            string Str(string k) => r.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";
            var version = Str("version");
            var url = Str("url");
            var sha = Str("sha256").ToLowerInvariant();
            long size = r.TryGetProperty("size", out var sz) && sz.ValueKind == JsonValueKind.Number ? sz.GetInt64() : 0;
            if (ParseVersion(version) is null || url.Length == 0 || !Sha256Hex().IsMatch(sha))
                throw new InvalidOperationException("Манифест обновлений повреждён");
            RequireInstallerUrl(url);
            return new UpdateInfo(version, Str("notes"), url, sha, size);
        }
        catch (JsonException)
        {
            throw new InvalidOperationException("Манифест обновлений повреждён");
        }
    }

    private static void RequireOwnHost(string url, string what)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var u) || u.Scheme != Uri.UriSchemeHttps
            || !AllowedHosts.Contains(u.Host, StringComparer.OrdinalIgnoreCase) || !u.IsDefaultPort || u.UserInfo.Length > 0)
            throw new InvalidOperationException($"Адрес {what} не на GitHub");
    }

    /** Установщик — только из выпусков Repo на github.com: проверяем разобранный адрес, а не строку (обход через ..). */
    private static void RequireInstallerUrl(string url)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var u) || u.Scheme != Uri.UriSchemeHttps
            || !string.Equals(u.Host, "github.com", StringComparison.OrdinalIgnoreCase) || !u.IsDefaultPort || u.UserInfo.Length > 0
            || !u.AbsolutePath.StartsWith(InstallerPath, StringComparison.Ordinal) || u.AbsolutePath.Contains("..")
            || url.Contains("..") || url.Contains("%2e", StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("Установщик не из выпусков " + Repo);
    }

    private static Version? ParseVersion(string v)
    {
        var core = v.Split('-', '+')[0];
        return Version.TryParse(core, out var r) ? r : null;
    }

    private static bool IsNewer(string candidate, string current)
        => ParseVersion(candidate) is { } a && ParseVersion(current) is { } b && a > b;

    private static void TryDelete(string path)
    {
        try { File.Delete(path); } catch (IOException) { } catch (UnauthorizedAccessException) { }
    }

    [GeneratedRegex("^[A-Za-z0-9._-]+$")]
    private static partial Regex SafeName();
    [GeneratedRegex("^[0-9a-f]{64}$")]
    private static partial Regex Sha256Hex();
}

/** Проверка подписи ed25519 (BouncyCastle: в .NET нет встроенного Ed25519). */
public static class ManifestSignature
{
    /** data — точные байты файла; signatureB64 — base64 от 64 байт; publicKeyB64 — base64 от 32 байт. Любая ошибка — false. */
    public static bool Verify(byte[] data, string signatureB64, string publicKeyB64)
    {
        try
        {
            var sig = Convert.FromBase64String(signatureB64.Trim());
            var key = Convert.FromBase64String(publicKeyB64.Trim());
            if (sig.Length != Ed25519.SignatureSize || key.Length != Ed25519.PublicKeySize) return false;
            return Ed25519.Verify(sig, 0, key, 0, data, 0, data.Length);
        }
        catch (Exception e) when (e is FormatException or ArgumentException or InvalidOperationException)
        {
            return false;
        }
    }
}
