using System.Text.Json;
using PiMusic.Data;

namespace PiMusic.Views;

/** Строка списка импортов: данные ImportJob и всё, что нужно разметке (прогресс, кнопки по состоянию). */
public sealed class ImportRow
{
    public ImportRow(ImportJob j)
    {
        Job = j;
        var done = Count(j.Counts, "done") + Count(j.Counts, "cached");
        Failed = Count(j.Counts, "failed");
        HasProgress = j.Total > 0 && j.Counts is not null;
        Fraction = j.Total > 0 ? Math.Clamp((double)(done + Failed) / j.Total, 0, 1) : 0;
    }

    public ImportJob Job { get; }
    public long Id => Job.Id;
    public string Filename => string.IsNullOrEmpty(Job.Filename) ? "Список треков" : Job.Filename;
    public string Text => Job.Text;
    public int Failed { get; }
    public double Fraction { get; }
    public bool HasProgress { get; }
    public bool Running => Job.State == "running";
    public bool Paused => Job.State == "paused";
    public bool Active => Running || Paused;
    public bool HasFailed => Failed > 0;
    public string FailedText => $"Не найдено ({Failed})";

    /** Отпечаток для сравнения: перерисовывать список, только если что-то поменялось. */
    public string Key => $"{Job.Id}:{Job.State}:{Job.Text}:{Fraction:F3}:{Failed}";

    private static int Count(JsonElement? counts, string key) =>
        counts is { ValueKind: JsonValueKind.Object } c && c.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.Number
            && v.TryGetInt32(out var n) ? n : 0;
}
