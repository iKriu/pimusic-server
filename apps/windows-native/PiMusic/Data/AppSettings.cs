using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace PiMusic.Data;

/**
 * Локальные настройки приложения: %LOCALAPPDATA%\PiMusic\settings.json (распакованному приложению
 * ApplicationData недоступна). Токен устройства хранится зашифрованным DPAPI (только для этого пользователя Windows).
 * Запись — атомарно (временный файл + замена).
 */
public sealed class AppSettings
{
    /** Адрес сервера не задан: при первом запуске его вводит пользователь (свой сервер PiMusic). */
    public const string DefaultServer = "";

    public static string Dir { get; } = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "PiMusic");
    private static string FilePath => Path.Combine(Dir, "settings.json");
    private static readonly Lock Gate = new();

    public static AppSettings Current { get; } = Load();

    public string Server { get; set; } = DefaultServer;
    /** токен устройства, зашифрованный DPAPI (base64) */
    public string? TokenProtected { get; set; }
    public string? Login { get; set; }
    public double Volume { get; set; } = 0.8;
    public bool Normalize { get; set; } = true;
    public bool CloseToTray { get; set; } = true;
    public bool TrackNotifications { get; set; }
    /** "system" | "light" | "dark" */
    public string Theme { get; set; } = "system";
    /** id устройства для ключей eq.device.<id> (свой у каждой установки) */
    public string? DeviceId { get; set; }
    public long LastUpdateCheck { get; set; }
    public string? SkippedVersion { get; set; }
    /** положение и размер окна: x, y, ширина, высота (физические пиксели) */
    public int[]? WindowRect { get; set; }
    public bool WindowMaximized { get; set; }

    /** Расшифрованный токен: в файл не пишется (только TokenProtected). */
    [System.Text.Json.Serialization.JsonIgnore]
    public string? Token
    {
        get
        {
            if (string.IsNullOrEmpty(TokenProtected)) return null;
            try
            {
                var raw = ProtectedData.Unprotect(Convert.FromBase64String(TokenProtected), Entropy, DataProtectionScope.CurrentUser);
                return Encoding.UTF8.GetString(raw);
            }
            catch (Exception) { return null; }
        }
        set => TokenProtected = string.IsNullOrEmpty(value) ? null
            : Convert.ToBase64String(ProtectedData.Protect(Encoding.UTF8.GetBytes(value), Entropy, DataProtectionScope.CurrentUser));
    }

    // Свойство, а не поле: Current = Load() выполняется раньше полей, объявленных ниже.
    private static ReadOnlySpan<byte> EntropyBytes => "PiMusic.device-token"u8;
    private static byte[] Entropy => EntropyBytes.ToArray();

    private static AppSettings Load()
    {
        try
        {
            if (File.Exists(FilePath))
                return JsonSerializer.Deserialize<AppSettings>(File.ReadAllText(FilePath), Json.Options) ?? new AppSettings();
        }
        catch (Exception) { /* испорченный файл — начинаем с настроек по умолчанию */ }
        return new AppSettings();
    }

    public void Save()
    {
        lock (Gate)
        {
            Directory.CreateDirectory(Dir);
            var tmp = FilePath + ".tmp";
            File.WriteAllText(tmp, JsonSerializer.Serialize(this, Json.Options));
            File.Move(tmp, FilePath, overwrite: true);
        }
    }
}
