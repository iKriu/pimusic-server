using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Navigation;
using PiMusic.Data;
using PiMusic.Player;
using Windows.ApplicationModel.DataTransfer;
using Windows.Storage;
using Windows.Storage.Pickers;

namespace PiMusic.Views;

/**
 * «Загрузки»: три вкладки. Скачивание — ход (ActivityWatcher), журнал и скачивание по ссылке.
 * Свои файлы — отправка аудио порциями с прогрессом, need_info, список загрузок. Импорт — списки TXT/CSV/TSV.
 * Файлы можно перетащить на страницу. Списки загрузок и импортов опрашиваются, пока есть незавершённое и страница открыта.
 */
public sealed partial class DownloadsPage : Page
{
    private static readonly string[] AudioExt = [".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac"];
    private static readonly string[] ListExt = [".txt", ".csv", ".tsv"];
    private const int Chunk = 50;
    private const long MaxList = 2 * 1024 * 1024;

    private readonly DispatcherTimer _poll = new() { Interval = TimeSpan.FromSeconds(2.5) };
    private readonly Dictionary<long, UploadInfoCard> _cards = [];
    private CancellationTokenSource? _send;
    private bool _open, _refreshing;
    private string _uploadsKey = "", _importsKey = "";

    public DownloadsPage()
    {
        InitializeComponent();
        _poll.Tick += async (_, _) => await Refresh();
    }

    protected override void OnNavigatedTo(NavigationEventArgs e)
    {
        _open = true;
        ActivityWatcher.Watching = true;
        ActivityWatcher.Changed += Show;
        if (ActivityWatcher.Last is { } a) Show(a);
        ActivityWatcher.Poke();
        _ = Refresh();
    }

    protected override void OnNavigatedFrom(NavigationEventArgs e)
    {
        _open = false;
        _poll.Stop();
        ActivityWatcher.Watching = false;
        ActivityWatcher.Changed -= Show;
    }

    // ---------------------------------------------------------------- вкладки

    private void Tabs_SelectionChanged(SelectorBar sender, SelectorBarSelectionChangedEventArgs args) => ShowTab(sender.SelectedItem);

    private void ShowTab(SelectorBarItem? item)
    {
        DownloadPanel.Visibility = Ui.Show(item == TabDownload || item is null);
        UploadsPanel.Visibility = Ui.Show(item == TabUploads);
        ImportPanel.Visibility = Ui.Show(item == TabImport);
    }

    private void Select(SelectorBarItem item)
    {
        Tabs.SelectedItem = item;
        ShowTab(item);
    }

    // ---------------------------------------------------------------- скачивание

    private void Show(Activity a)
    {
        ActiveList.ItemsSource = a.ActiveItems;
        IdleText.Visibility = a.ActiveItems.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        QueueText.Text = a.Queue > 0 ? $"В очереди ещё: {Fmt.Tracks(a.Queue)}" : "";
        QueueText.Visibility = a.Queue > 0 ? Visibility.Visible : Visibility.Collapsed;
        LogList.ItemsSource = a.Log.OrderByDescending(l => l.Ts).ToList();
        LogEmpty.Visibility = a.Log.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
    }

    public static string Clock(long ts) => ts <= 0 ? "" : DateTimeOffset.FromUnixTimeSeconds(ts).ToLocalTime().ToString("HH:mm");

    private void LinkBox_KeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key == Windows.System.VirtualKey.Enter) Link_Click(sender, e);
    }

    private async void Link_Click(object sender, RoutedEventArgs e)
    {
        var url = LinkBox.Text.Trim();
        if (url.Length == 0) return;
        try
        {
            var r = await Api.Instance.DownloadLink(url);
            Ui.Success(Catalog.Queued(r));
            LinkBox.Text = "";
            ActivityWatcher.Poke();
        }
        catch (Exception ex) { Ui.Error(ex); }
    }

    // ---------------------------------------------------------------- опрос загрузок и импортов

    /** Обновить оба списка; таймер работает, только пока есть незавершённое и страница открыта. */
    private async Task Refresh()
    {
        if (_refreshing || !_open) return;
        _refreshing = true;
        var pending = false;
        try
        {
            var up = Api.Instance.Uploads();
            var im = Api.Instance.Imports();
            try { pending |= ShowUploads((await up).Items); } catch (Exception) { /* следующая попытка — по таймеру */ pending = true; }
            try { pending |= ShowImports((await im).Imports); } catch (Exception) { pending = true; }
        }
        finally { _refreshing = false; }
        if (!_open) return;
        if (pending || _send is not null) _poll.Start(); else _poll.Stop();
    }

    /** Заново запустить опрос (после действия пользователя). */
    private void Kick() => _ = Refresh();

    private bool ShowUploads(List<Upload> items)
    {
        // need_info — карточки с полями: живут, пока загрузка ждёт, чтобы опрос не сбрасывал ввод
        var need = items.Where(u => u.NeedInfo).ToList();
        foreach (var id in _cards.Keys.Where(id => need.All(u => u.Id != id)).ToList())
        {
            NeedInfoList.Children.Remove(_cards[id]);
            _cards.Remove(id);
        }
        foreach (var u in need.Where(u => !_cards.ContainsKey(u.Id)))
        {
            var card = new UploadInfoCard(u, Kick);
            _cards[u.Id] = card;
            NeedInfoList.Children.Add(card);
        }
        NeedInfoSection.Visibility = Ui.Show(need.Count > 0);
        TabUploads.Text = need.Count > 0 ? $"Свои файлы ({need.Count})" : "Свои файлы";

        var rest = items.Where(u => !u.NeedInfo).ToList();
        var key = string.Join(",", rest.Select(u => $"{u.Id}:{u.State}:{u.UpdatedAt}:{u.Track?.Id}"));
        if (key != _uploadsKey)
        {
            _uploadsKey = key;
            UploadList.ItemsSource = rest;
        }
        UploadsEmpty.Visibility = Ui.Show(items.Count == 0);
        // need_info ждёт человека, а не сервер — опрашивать ради неё не нужно
        return rest.Any(u => !u.Finished);
    }

    private bool ShowImports(List<ImportJob> jobs)
    {
        var rows = jobs.Select(j => new ImportRow(j)).ToList();
        var key = string.Join(",", rows.Select(r => r.Key));
        if (key != _importsKey)
        {
            _importsKey = key;
            ImportList.ItemsSource = rows;
        }
        ImportsEmpty.Visibility = Ui.Show(rows.Count == 0);
        var running = rows.Count(r => r.Active);
        TabImport.Text = running > 0 ? $"Импорт ({running})" : "Импорт";
        return rows.Any(r => r.Running);
    }

    // ---------------------------------------------------------------- свои файлы: строки списка

    public static string UploadTitle(Track? t, string name) => t is null ? name : $"{t.Artist} — {t.Title}";

    public static string UploadSub(string state, string stateText, string? note) =>
        Fmt.Join(string.IsNullOrEmpty(stateText) ? state : stateText, note);

    public static bool Busy(string state) => state is "receiving" or "queued" or "analyzing" or "processing";
    public static Visibility ShowBusy(string state) => Ui.Show(Busy(state));
    public static Visibility ShowGlyph(string state) => Ui.Show(!Busy(state));
    public static Visibility ShowCancel(string state) => Ui.Show(Busy(state));

    public static string StateGlyph(string state) => state switch
    {
        "done" => "",
        "duplicate" => "",
        "failed" => "",
        "cancelled" => "",
        "need_info" => "",
        _ => "",
    };

    private static Upload? ItemOf(object sender) => (sender as FrameworkElement)?.DataContext as Upload;

    private void UploadPlay_Click(object sender, RoutedEventArgs e) => Play(ItemOf(sender));

    private void UploadRow_DoubleTapped(object sender, DoubleTappedRoutedEventArgs e) => Play(ItemOf(sender));

    private static void Play(Upload? u)
    {
        if (u?.Track is { } t) PlayerService.Instance.Play([t]);
    }

    private async void UploadCancel_Click(object sender, RoutedEventArgs e)
    {
        if (ItemOf(sender) is not { } u) return;
        try { await Api.Instance.UploadCancel(u.Id); Kick(); }
        catch (Exception ex) { Ui.Error(ex); }
    }

    // ---------------------------------------------------------------- выбор и перетаскивание файлов

    private static FileOpenPicker Picker(IEnumerable<string> ext)
    {
        var p = new FileOpenPicker { SuggestedStartLocation = PickerLocationId.MusicLibrary, ViewMode = PickerViewMode.List };
        foreach (var e in ext) p.FileTypeFilter.Add(e);
        WinRT.Interop.InitializeWithWindow.Initialize(p, WinRT.Interop.WindowNative.GetWindowHandle(App.Window));
        return p;
    }

    private async void PickAudio_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var files = await Picker(AudioExt).PickMultipleFilesAsync();
            if (files is { Count: > 0 }) await SendAudio(files.Select(f => f.Path).Where(p => p.Length > 0).ToList());
        }
        catch (Exception ex) { Ui.Error(ex); }
    }

    private async void PickList_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var files = await Picker(ListExt).PickMultipleFilesAsync();
            if (files is { Count: > 0 }) await SendLists(files.Select(f => f.Path).Where(p => p.Length > 0).ToList());
        }
        catch (Exception ex) { Ui.Error(ex); }
    }

    private static bool Has(DragEventArgs e) => e.DataView.Contains(StandardDataFormats.StorageItems);

    private void Page_DragEnter(object sender, DragEventArgs e)
    {
        if (Has(e)) DropHint.Visibility = Visibility.Visible;
    }

    private void Page_DragOver(object sender, DragEventArgs e)
    {
        if (!Has(e)) { e.AcceptedOperation = DataPackageOperation.None; return; }
        e.AcceptedOperation = DataPackageOperation.Copy;
        e.DragUIOverride.Caption = "Загрузить в PiMusic";
        e.DragUIOverride.IsCaptionVisible = true;
    }

    private void Page_DragLeave(object sender, DragEventArgs e) => DropHint.Visibility = Visibility.Collapsed;

    private async void Page_Drop(object sender, DragEventArgs e)
    {
        DropHint.Visibility = Visibility.Collapsed;
        if (!Has(e)) return;
        var def = e.GetDeferral();
        List<string> paths;
        try
        {
            var items = await e.DataView.GetStorageItemsAsync();
            paths = items.OfType<StorageFile>().Select(f => f.Path).Where(p => !string.IsNullOrEmpty(p)).ToList();
        }
        catch (Exception ex) { Ui.Error(ex); return; }
        finally { def.Complete(); }

        var audio = paths.Where(p => AudioExt.Contains(Ext(p))).ToList();
        var lists = paths.Where(p => ListExt.Contains(Ext(p))).ToList();
        var skipped = paths.Count - audio.Count - lists.Count;
        if (audio.Count == 0 && lists.Count == 0)
        {
            Ui.Info("Подходят аудиофайлы (mp3, flac, m4a, ogg, opus, wav, aac) и списки TXT/CSV/TSV");
            return;
        }
        if (skipped > 0) Ui.Info($"Пропущено файлов неподходящего типа: {skipped}");
        if (lists.Count > 0) await SendLists(lists);
        if (audio.Count > 0) await SendAudio(audio);
    }

    private static string Ext(string path) => Path.GetExtension(path).ToLowerInvariant();

    // ---------------------------------------------------------------- отправка аудио

    /** Отправить аудио порциями по 50 файлов: общий прогресс по байтам, отмена, сводка по отклонённым. */
    private async Task SendAudio(List<string> files)
    {
        Select(TabUploads);
        if (_send is not null) { Ui.Info("Дождитесь окончания текущей отправки"); return; }
        files = files.Distinct(StringComparer.OrdinalIgnoreCase).Where(File.Exists).ToList();
        if (files.Count == 0) return;

        var cts = _send = new CancellationTokenSource();
        var total = files.Sum(f => new FileInfo(f).Length);
        long before = 0;
        var accepted = 0;
        var problems = new List<string>();
        string? stop = null;
        SendResult.IsOpen = false;
        SendPanel.Visibility = Visibility.Visible;
        PickAudioButton.IsEnabled = false;
        ReportSent(0, total, files.Count);
        _poll.Start();
        try
        {
            foreach (var chunk in files.Chunk(Chunk))
            {
                var offset = before;
                var progress = new Progress<(long Sent, long Total)>(p => ReportSent(offset + p.Sent, total, files.Count));
                var r = await Api.Instance.Upload(chunk, progress, cts.Token);
                accepted += r.Uploads.Count;
                problems.AddRange(r.Rejected.Select(x => $"{x.Name}: {(string.IsNullOrEmpty(x.Message) ? x.Error : x.Message)}"));
                before += chunk.Sum(f => new FileInfo(f).Length);
                Kick();
                if (r.Error is not null)
                {
                    // 413/415/429/507: сервер принял часть — дальше не шлём
                    stop = string.IsNullOrEmpty(r.Message) ? r.Error : r.Message;
                    break;
                }
            }
            ShowSendResult(accepted, files.Count, problems, stop, false);
        }
        catch (OperationCanceledException) { ShowSendResult(accepted, files.Count, problems, stop, true); }
        catch (Exception ex)
        {
            Ui.Error(ex);
            if (accepted > 0 || problems.Count > 0) ShowSendResult(accepted, files.Count, problems, stop, false);
        }
        finally
        {
            _send = null;
            cts.Dispose();
            SendPanel.Visibility = Visibility.Collapsed;
            PickAudioButton.IsEnabled = true;
            Kick();
        }
    }

    private void ReportSent(long sent, long total, int count)
    {
        SendBar.Value = total > 0 ? Math.Clamp((double)sent / total, 0, 1) : 0;
        SendText.Text = $"Отправка {Files(count)}: {Mb(sent)} из {Mb(total)} МБ";
    }

    private static string Mb(long bytes) => (bytes / 1048576.0).ToString(bytes < 10 * 1048576 ? "0.0" : "0");

    private static string Files(int n)
    {
        var m10 = n % 10; var m100 = n % 100;
        var w = m10 == 1 && m100 != 11 ? "файла" : "файлов";
        return $"{n} {w}";
    }

    private void ShowSendResult(int accepted, int count, List<string> problems, string? stop, bool cancelled)
    {
        var title = cancelled ? $"Отправка отменена. Принято {accepted} из {count}"
            : accepted == count ? $"Принято файлов: {accepted}" : $"Принято {accepted} из {count}";
        var lines = new List<string>();
        if (stop is not null) lines.Add(stop);
        if (problems.Count > 0) lines.Add("Не приняты: " + string.Join("; ", problems.Take(10)) + (problems.Count > 10 ? $" и ещё {problems.Count - 10}" : ""));
        SendResult.Title = title;
        SendResult.Message = string.Join(Environment.NewLine, lines);
        SendResult.Severity = cancelled ? InfoBarSeverity.Informational
            : stop is not null || problems.Count > 0 ? (accepted > 0 ? InfoBarSeverity.Warning : InfoBarSeverity.Error)
            : InfoBarSeverity.Success;
        SendResult.IsOpen = true;
    }

    private void CancelSend_Click(object sender, RoutedEventArgs e) => _send?.Cancel();

    // ---------------------------------------------------------------- импорт списков

    private async Task SendLists(List<string> files)
    {
        Select(TabImport);
        PickListButton.IsEnabled = false;
        try
        {
            foreach (var f in files.Distinct(StringComparer.OrdinalIgnoreCase))
            {
                var name = Path.GetFileName(f);
                if (!File.Exists(f)) continue;
                if (new FileInfo(f).Length > MaxList) { ShowImportResult(InfoBarSeverity.Error, $"«{name}» больше 2 МБ", "", null); continue; }
                try
                {
                    var r = await Api.Instance.ImportFile(f);
                    var msg = string.IsNullOrEmpty(r.Format) ? Fmt.Tracks(r.Total) : $"{Fmt.Tracks(r.Total)} · формат {r.Format}";
                    if (r.Playlist is not null) msg += $" → плейлист «{r.Playlist.Name}»";
                    ShowImportResult(InfoBarSeverity.Success, $"Импорт «{(string.IsNullOrEmpty(r.Name) ? name : r.Name)}» начат", msg, r.Playlist);
                }
                catch (Exception ex)
                {
                    ShowImportResult(InfoBarSeverity.Error, $"«{name}» не принят", ex is ApiException ? ex.Message : "Что-то пошло не так: " + ex.Message, null);
                }
            }
        }
        finally
        {
            PickListButton.IsEnabled = true;
            Kick();
        }
    }

    private void ShowImportResult(InfoBarSeverity severity, string title, string message, PlRef? playlist)
    {
        ImportResult.Severity = severity;
        ImportResult.Title = title;
        ImportResult.Message = message;
        if (playlist is not null)
        {
            var b = new HyperlinkButton { Content = "Открыть плейлист" };
            var id = playlist.Id;
            b.Click += (_, _) => App.Window?.Navigate(typeof(PlaylistPage), id);
            ImportResult.ActionButton = b;
        }
        else ImportResult.ActionButton = null;
        ImportResult.IsOpen = true;
    }

    private static ImportRow? RowOf(object sender) => (sender as FrameworkElement)?.DataContext as ImportRow;

    private async void ImportPause_Click(object sender, RoutedEventArgs e)
    {
        if (RowOf(sender) is { } r) await ImportAct(() => Api.Instance.ImportPause(r.Id));
    }

    private async void ImportResume_Click(object sender, RoutedEventArgs e)
    {
        if (RowOf(sender) is { } r) await ImportAct(() => Api.Instance.ImportResume(r.Id));
    }

    private async void ImportCancel_Click(object sender, RoutedEventArgs e)
    {
        if (RowOf(sender) is not { } r) return;
        if (!await Ui.Confirm(XamlRoot, "Отменить импорт?", $"Оставшиеся треки из «{r.Filename}» скачиваться не будут. Уже скачанные останутся.", "Отменить импорт"))
            return;
        await ImportAct(() => Api.Instance.ImportCancel(r.Id));
    }

    private async Task ImportAct(Func<Task> call)
    {
        try { await call(); Kick(); }
        catch (Exception ex) { Ui.Error(ex); }
    }

    private async void ImportFailed_Click(object sender, RoutedEventArgs e)
    {
        if (RowOf(sender) is { } r) await ImportFailedDialog.Show(XamlRoot, r.Id, r.Filename);
    }
}
