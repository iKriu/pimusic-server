using System.Collections.ObjectModel;
using System.Collections.Specialized;
using CommunityToolkit.Mvvm.ComponentModel;
using Microsoft.UI.Dispatching;
using PiMusic.Data;
using Windows.Media;
using Windows.Media.Core;
using Windows.Media.Playback;
using Windows.Storage.Streams;

namespace PiMusic.Player;

public enum RepeatMode { Off, All, One }

/**
 * Плеер: очередь на клиенте, Windows.Media.Playback.MediaPlayer, системные медиаэлементы (SMTC: медиаклавиши,
 * всплывающая панель громкости, экран блокировки), нормализация громкости по gain/peak, сохранение очереди.
 * Все свойства меняются в потоке интерфейса (Init(DispatcherQueue)); события MediaPlayer и SMTC приходят
 * из фоновых потоков и переносятся туда через _ui.
 */
public sealed partial class PlayerService : ObservableObject
{
    public static PlayerService Instance { get; } = new();

    public ObservableCollection<Track> Queue { get; } = [];

    [ObservableProperty] public partial int Index { get; set; } = -1;
    [ObservableProperty] public partial Track? Current { get; set; }
    [ObservableProperty] public partial bool IsPlaying { get; set; }
    [ObservableProperty] public partial bool IsBuffering { get; set; }
    /** секунды */
    [ObservableProperty] public partial double Position { get; set; }
    /** секунды (0 — неизвестна) */
    [ObservableProperty] public partial double Duration { get; set; }
    /** 0…1 — громкость пользователя (нормализация применяется поверх) */
    [ObservableProperty] public partial double Volume { get; set; } = 0.8;
    [ObservableProperty] public partial bool IsMuted { get; set; }
    [ObservableProperty] public partial bool Shuffle { get; set; }
    [ObservableProperty] public partial RepeatMode Repeat { get; set; }
    /** текст последней ошибки воспроизведения (null — нет) */
    [ObservableProperty] public partial string? Error { get; set; }

    public bool HasPrevious => !IsLive && (Index > 0 || Position > 3);
    public bool HasNext => !IsLive && Index >= 0 && (Index < Queue.Count - 1 || Repeat == RepeatMode.All);

    private DispatcherQueue? _ui;
    /** держатель системных медиаэлементов (SMTC) и обычный движок */
    private MediaPlayer? _player;
    private MediaPlayerEngine? _mpe;
    /** движок с эквалайзером (AudioGraph), создаётся по требованию */
    private GraphEngine? _ge;
    /** движок текущего источника */
    private IAudioEngine? _engine;
    private readonly EqDsp _dsp = new();
    private EqState _eq = EqState.Flat;
    /** AudioGraph не смог открыть текущий источник — до следующей загрузки играем через MediaPlayer */
    private bool _forceMp;
    /** граф не собрался или не открыл источник — до этого времени (UTC) не пробовать его снова */
    private DateTime _graphDownUntil;
    /** переподключений эфира подряд без нормального проигрывания */
    private int _liveRetries;
    private DateTime _lastRebuild;
    private SystemMediaTransportControls? _smtc;
    private DispatcherQueueTimer? _tick;
    private DispatcherQueueTimer? _saveTimer;
    private DispatcherQueueTimer? _settingsTimer;

    /** номер загрузки: события от прежнего источника отбрасываются */
    private int _gen;
    /** пользователь хочет, чтобы играло (в том числе пока трек открывается) */
    private bool _wantPlay;
    /** позиция, на которую перейти после открытия трека (восстановление, повтор после ошибки) */
    private double _pendingSeek;
    /** на этом треке уже обновляли ссылку после ошибки */
    private bool _retried;
    /** ошибок подряд (защита от зацикливания на битой очереди) */
    private int _failStreak;
    private bool _lastHasPrevious;
    private bool _quietShuffle;
    private int _ticks;
    private bool _shutDown;
    /** пакетная правка очереди: уведомления и сохранение — один раз в конце */
    private bool _batch;
    /** восстановление закончено — до этого queue.json не перезаписываем */
    private bool _restored;
    /** номер сохранения: устаревшая фоновая запись не затирает более новую */
    private long _saveSeq;
    private static long _writtenSeq;

    private static readonly Lock SaveGate = new();
    private static string QueuePath => Path.Combine(AppSettings.Dir, "queue.json");
    private const long RefreshAfter = 6 * 3600;

    private PlayerService()
    {
        Queue.CollectionChanged += OnQueueChanged;
    }

    /** Вызвать один раз при старте из потока интерфейса: MediaPlayer, SMTC, восстановление очереди. */
    public void Init(DispatcherQueue ui)
    {
        if (_ui is not null) return;
        _ui = ui;

        _player = new MediaPlayer { AudioCategory = MediaPlayerAudioCategory.Media, AutoPlay = false };
        _player.CommandManager.IsEnabled = false;
        _mpe = new MediaPlayerEngine(_player);
        Hook(_mpe);
        _engine = _mpe;

        _smtc = _player.SystemMediaTransportControls;
        _smtc.IsEnabled = true;
        _smtc.IsPlayEnabled = true;
        _smtc.IsPauseEnabled = true;
        _smtc.IsStopEnabled = true;
        _smtc.IsNextEnabled = false;
        _smtc.IsPreviousEnabled = false;
        _smtc.PlaybackStatus = MediaPlaybackStatus.Closed;
        _smtc.ButtonPressed += (_, e) => { var b = e.Button; _ui.TryEnqueue(() => OnSmtcButton(b)); };
        _smtc.PlaybackPositionChangeRequested += (_, e) =>
        {
            var p = e.RequestedPlaybackPosition.TotalSeconds;
            _ui.TryEnqueue(() => Seek(p));
        };
        _smtc.ShuffleEnabledChangeRequested += (_, e) => { var v = e.RequestedShuffleEnabled; _ui.TryEnqueue(() => Shuffle = v); };
        _smtc.AutoRepeatModeChangeRequested += (_, e) =>
        {
            var m = e.RequestedAutoRepeatMode switch
            {
                MediaPlaybackAutoRepeatMode.Track => RepeatMode.One,
                MediaPlaybackAutoRepeatMode.List => RepeatMode.All,
                _ => RepeatMode.Off,
            };
            _ui.TryEnqueue(() => Repeat = m);
        };

        _tick = ui.CreateTimer();
        _tick.Interval = TimeSpan.FromMilliseconds(250);
        _tick.Tick += (_, _) => OnTick();
        _saveTimer = ui.CreateTimer();
        _saveTimer.Interval = TimeSpan.FromSeconds(1);
        _saveTimer.IsRepeating = false;
        _saveTimer.Tick += (_, _) => SaveQueue(sync: false);
        _settingsTimer = ui.CreateTimer();
        _settingsTimer.Interval = TimeSpan.FromMilliseconds(500);
        _settingsTimer.IsRepeating = false;
        _settingsTimer.Tick += (_, _) => SaveSettings();

        Volume = Math.Clamp(AppSettings.Current.Volume, 0, 1);
        ApplyVolume();
        _ = Restore();
    }

    // ---------------------------------------------------------------- управление

    /** Заменить очередь и начать с start (shuffle — перемешать, start становится первым). */
    public void Play(IReadOnlyList<Track> tracks, int start = 0, bool shuffle = false)
    {
        if (tracks.Count == 0) return;
        LeaveLive();
        start = Math.Clamp(start, 0, tracks.Count - 1);
        var list = tracks.ToList();
        if (shuffle || Shuffle)
        {
            // shuffle: true и start = 0 — всё вперемешку; иначе выбранный трек первым, остальные вперемешку
            if (shuffle && start == 0)
            {
                var all = list.ToArray();
                Random.Shared.Shuffle(all);
                list = [.. all];
            }
            else
            {
                var first = list[start];
                list.RemoveAt(start);
                var rest = list.ToArray();
                Random.Shared.Shuffle(rest);
                list = [first, .. rest];
            }
            start = 0;
            if (!Shuffle) { _quietShuffle = true; Shuffle = true; _quietShuffle = false; }
        }
        Error = null;
        _failStreak = 0;
        Batch(() =>
        {
            Queue.Clear();
            foreach (var t in list) Queue.Add(t);
        });
        Load(start, play: true);
    }

    // ---------------------------------------------------------------- прямой эфир

    /** null — обычная очередь; "wave" — «Моя Волна»; "radio" — интернет-радио. */
    [ObservableProperty] public partial string? LiveKind { get; set; }
    /** Что в эфире «Моей Волны» (опрос раз в 15 с, пока она играет). */
    [ObservableProperty] public partial WaveNow? Wave { get; set; }

    public bool IsLive => LiveKind is not null;

    private DispatcherQueueTimer? _waveTimer;

    /** «Моя Волна»: бесконечный MP3-поток stream_url из /api/me (очередь заменяется, на диске остаётся прежняя). */
    public void PlayWave(Uri stream) => PlayLive(new Track
    {
        Id = -1, Title = "Моя Волна", Artist = "Бесконечный эфир из медиатеки", StreamUri = stream, Live = true,
    }, "wave");

    /** Интернет-радио: поток станции. */
    public void PlayRadio(Station s)
    {
        if (!Uri.TryCreate(s.Url, UriKind.Absolute, out var u)) { Error = "У станции неверный адрес потока"; return; }
        PlayLive(new Track { Id = -2 - s.Id, Title = s.Name, Artist = "Интернет-радио", StreamUri = u, Live = true }, "radio");
    }

    private void PlayLive(Track t, string kind)
    {
        Error = null;
        _failStreak = 0;
        _liveRetries = 0;
        Wave = null;
        LiveKind = kind;
        if (Shuffle) { _quietShuffle = true; Shuffle = false; _quietShuffle = false; }
        Batch(() =>
        {
            Queue.Clear();
            Queue.Add(t);
        });
        Load(0, play: true);
        if (kind == "wave") StartWavePoll();
        else StopWavePoll();
    }

    /** Выйти из эфира перед обычным воспроизведением. */
    private void LeaveLive()
    {
        if (!IsLive) return;
        StopWavePoll();
        LiveKind = null;
        Wave = null;
    }

    partial void OnLiveKindChanged(string? value) => NotifyNav();

    private void StartWavePoll()
    {
        if (_ui is null) return;
        if (_waveTimer is null)
        {
            _waveTimer = _ui.CreateTimer();
            _waveTimer.Interval = TimeSpan.FromSeconds(15);
            _waveTimer.Tick += (_, _) => _ = PollWave();
        }
        _waveTimer.Start();
        _ = PollWave();
    }

    private void StopWavePoll() => _waveTimer?.Stop();

    /** Обновить «что в эфире»: подпись трека, обложка и системные медиаэлементы — без переоткрытия потока. */
    private async Task PollWave()
    {
        WaveNow now;
        try { now = await Api.Instance.WaveNow(); }
        catch (Exception) { return; }
        if (LiveKind != "wave" || Current is not { Live: true } cur) return;
        Wave = now;
        if (string.IsNullOrEmpty(now.Title) || (cur.Title == now.Title && cur.Artist == now.Artist)) return;
        var t = new Track
        {
            Id = cur.Id, Title = now.Title, Artist = now.Artist ?? "", Album = now.Album ?? "", CoverUri = now.CoverUri,
            StreamUri = cur.StreamUri, Live = true,
        };
        // и в очереди: переоткрытие эфира (после паузы) не вернёт заглушку «Моя Волна»
        if (Index >= 0 && Index < Queue.Count && Queue[Index].Live) Batch(() => Queue[Index] = t);
        Current = t;
        UpdateSmtcDisplay(t);
    }

    /** Вставить сразу после текущего. */
    public void PlayNext(Track track)
    {
        if (IsLive) { Play([track]); return; }
        if (Index < 0 || Queue.Count == 0) { Play([track]); return; }
        Queue.Insert(Index + 1, track);
    }

    /** Вставить по порядку сразу после текущего (пустая очередь — Play(список)). */
    public void PlayNext(IEnumerable<Track> tracks)
    {
        var list = tracks.ToList();
        if (list.Count == 0) return;
        if (IsLive || Index < 0 || Queue.Count == 0) { Play(list); return; }
        var at = Index + 1;
        Batch(() => { foreach (var t in list) Queue.Insert(at++, t); });
    }

    /** В конец очереди (если очередь была пуста — начать играть). */
    public void Enqueue(IEnumerable<Track> tracks)
    {
        if (IsLive) { Play(tracks.ToList()); return; }
        var wasEmpty = Queue.Count == 0;
        Batch(() => { foreach (var t in tracks) Queue.Add(t); });
        if (wasEmpty && Queue.Count > 0) Load(0, play: true);
    }

    public void TogglePlay()
    {
        if (_engine is null) return;
        if (Current is null)
        {
            if (Queue.Count > 0) Load(Math.Max(Index, 0), play: true);
            return;
        }
        if (!_engine.HasSource) { Load(Index, play: true, IsLive ? 0 : Position); return; }
        if (IsActive) { _wantPlay = false; _engine.Pause(); StopWavePoll(); }
        else
        {
            _wantPlay = true;
            Error = null;
            // эфир после паузы — с «сейчас», а не с места остановки
            if (IsLive)
            {
                Load(Index, play: true);
                if (LiveKind == "wave") StartWavePoll();
            }
            else _engine.Play();
        }
    }

    /** Играет на деле или открывается/подгружается с намерением играть. */
    private bool IsActive
    {
        get
        {
            if (_engine is null || !_engine.HasSource) return false;
            var st = _engine.State;
            return st == EngineState.Playing
                || (_wantPlay && st is EngineState.Opening or EngineState.Buffering or EngineState.Closed);
        }
    }

    public void Next()
    {
        if (Queue.Count == 0 || !HasNext) return;
        Load(Index + 1 < Queue.Count ? Index + 1 : 0, play: IsActive);
    }

    /** Больше 3 с от начала — в начало трека, иначе предыдущий. */
    public void Previous()
    {
        if (Queue.Count == 0) return;
        if (Position > 3 || Index < 0) { Seek(0); return; }
        if (Index > 0) Load(Index - 1, play: IsActive);
        else if (Repeat == RepeatMode.All) Load(Queue.Count - 1, play: IsActive);
        else Seek(0);
    }

    public void Seek(double seconds)
    {
        if (_engine is null || Current is null || IsLive) return;
        if (Duration > 0) seconds = Math.Min(seconds, Duration);
        seconds = Math.Max(0, seconds);
        if (!_engine.HasSource || _engine.State == EngineState.Opening)
            _pendingSeek = seconds;
        else
            _engine.Position = seconds;
        Position = seconds;
        UpdateTimeline();
    }

    public void JumpTo(int index)
    {
        if (index < 0 || index >= Queue.Count) return;
        Error = null;
        _failStreak = 0;
        Load(index, play: true);
    }

    public void RemoveAt(int index)
    {
        if (index < 0 || index >= Queue.Count) return;
        if (index != Index)
        {
            Queue.RemoveAt(index);
            if (index < Index) Index--;
            return;
        }
        Queue.RemoveAt(index);
        if (Queue.Count == 0) { ClearQueue(); return; }
        Load(Math.Min(index, Queue.Count - 1), play: IsActive && index < Queue.Count);
    }

    public void Move(int from, int to)
    {
        if (from == to || from < 0 || to < 0 || from >= Queue.Count || to >= Queue.Count) return;
        var i = Index;
        Queue.Move(from, to);
        if (from == i) Index = to;
        else if (from < i && to >= i) Index = i - 1;
        else if (from > i && to <= i) Index = i + 1;
    }

    public void ClearQueue()
    {
        _gen++;
        _wantPlay = false;
        _pendingSeek = 0;
        _engine?.Close();
        _tick?.Stop();
        StopWavePoll();
        LiveKind = null;
        Queue.Clear();
        Index = -1;
        Current = null;
        Position = 0;
        Duration = 0;
        IsPlaying = false;
        IsBuffering = false;
        if (_smtc is not null)
        {
            _smtc.DisplayUpdater.ClearAll();
            _smtc.DisplayUpdater.Update();
            _smtc.PlaybackStatus = MediaPlaybackStatus.Closed;
        }
        ScheduleSave();
    }

    /** Убрать из очереди удалённые с сервера треки (gone_ids). */
    public void RemoveGone(IEnumerable<long> ids)
    {
        var gone = ids.ToHashSet();
        if (gone.Count == 0 || !Queue.Any(t => gone.Contains(t.Id))) return;
        var cur = Index;
        var currentGone = cur >= 0 && cur < Queue.Count && gone.Contains(Queue[cur].Id);
        // новое место текущего (или следующего за ним) — сколько уцелело до него
        var newIndex = 0;
        for (var k = 0; k < cur && k < Queue.Count; k++)
            if (!gone.Contains(Queue[k].Id)) newIndex++;
        Batch(() =>
        {
            for (var k = Queue.Count - 1; k >= 0; k--)
                if (gone.Contains(Queue[k].Id)) Queue.RemoveAt(k);
        });

        if (Queue.Count == 0) { ClearQueue(); return; }
        if (cur < 0) return;
        if (!currentGone) { Index = newIndex; return; }
        if (newIndex < Queue.Count) Load(newIndex, play: IsActive);
        else if (Repeat == RepeatMode.All) Load(0, play: IsActive);
        else Load(Queue.Count - 1, play: false);
    }

    /** Перечитать AppSettings.Current.Normalize и применить громкость (после смены настройки). */
    public void RefreshVolume() => ApplyVolume();

    /** При выходе: сохранить очередь и позицию. */
    public void Shutdown()
    {
        if (_shutDown) return;
        _shutDown = true;
        if (_engine is not null && _engine.HasSource && _tick?.IsRunning == true && !IsLive)
            Position = _engine.Position;
        _tick?.Stop();
        _saveTimer?.Stop();
        if (_settingsTimer?.IsRunning == true) { _settingsTimer.Stop(); SaveSettings(); }
        SaveQueue(sync: true);
        StopWavePoll();
        _ge?.Dispose();
        _ge = null;
        _mpe?.Dispose();
        _player?.Dispose();
        _player = null;
        _engine = null;
    }

    // ---------------------------------------------------------------- загрузка трека

    private void Load(int i, bool play, double pos = 0, bool retry = false)
    {
        if (_mpe is null || _ui is null || i < 0 || i >= Queue.Count) return;
        var t = Queue[i];
        _gen++;
        _retried = retry;
        _wantPlay = play;
        _pendingSeek = pos;
        Index = i;
        Current = t;
        Position = pos;
        Duration = t.Seconds > 0 ? t.Seconds : 0;
        IsBuffering = true;
        IsPlaying = play;
        // таймер снова запустится на Playing; заодно запоздалая пауза старого источника не сбросит _wantPlay
        _tick?.Stop();

        // эквалайзер включён — через AudioGraph; если он не открыл этот трек (_forceMp) — обычным плеером
        if (!retry) _forceMp = false;
        IAudioEngine engine = _eq.Active && !_forceMp && DateTime.UtcNow >= _graphDownUntil ? _ge ??= NewGraph() : _mpe;
        if (!ReferenceEquals(engine, _engine)) _engine?.Close();
        _engine = engine;
        // скачанный на компьютер — с диска (играет и без сети)
        var uri = t.Live ? t.StreamUri : OfflineDownloads.Instance.LocalUri(t.Id) ?? t.StreamUri;
        if (uri is null)
        {
            engine.Close();
            var g = _gen;
            _ui.TryEnqueue(() => _ = OnFailed(g));
        }
        else engine.Open(uri, _gen);

        ApplyVolume();
        UpdateSmtcDisplay(t);
        UpdateTimeline();
        ScheduleSave();
    }

    /** События движка — в поток интерфейса; состояние учитываем только от текущего движка. */
    private void Hook(IAudioEngine e)
    {
        e.Opened += g => _ui?.TryEnqueue(() => OnOpened(g));
        e.Ended += g => _ui?.TryEnqueue(() => OnEnded(g));
        e.Failed += g => _ui?.TryEnqueue(() => _ = OnFailed(g));
        e.StateChanged += s => _ui?.TryEnqueue(() => { if (ReferenceEquals(s, _engine)) OnStateChanged(); });
    }

    private GraphEngine NewGraph()
    {
        var g = new GraphEngine(_dsp);
        Hook(g);
        // событие могло прийти от уже заменённого графа или пачкой — пересобираем только текущий и не чаще раза в 2 с
        g.DeviceLost += () => _ui?.TryEnqueue(() =>
        {
            if (!ReferenceEquals(g, _ge) || DateTime.UtcNow - _lastRebuild < TimeSpan.FromSeconds(2)) return;
            _lastRebuild = DateTime.UtcNow;
            RebuildGraph();
        });
        return g;
    }

    /** Устройство вывода сменилось: новый граф и тот же трек с той же позиции. */
    private void RebuildGraph()
    {
        if (_ge is null || _shutDown) return;
        var wasCurrent = ReferenceEquals(_engine, _ge);
        var play = IsActive;
        var pos = wasCurrent && _ge.HasSource ? CurrentPosition() : Position;
        _ge.Dispose();
        _ge = null;
        if (!wasCurrent) return;
        _engine = _mpe;
        if (Current is not null && Index >= 0) Load(Index, play, IsLive ? 0 : pos);
    }

    /**
     * Профиль эквалайзера (EqController): обработка меняется на лету. Звук начал меняться, а играет MediaPlayer —
     * текущий трек переоткрывается графом с той же позиции. Обратно (профиль стал ровным) — только со следующим
     * треком: граф с ровным профилем звучит так же, а перетаскивание ползунка через 0 не дёргает воспроизведение.
     */
    public void ApplyEq(EqState s)
    {
        _eq = s;
        _dsp.Set(s);
        if (!s.Active || Current is null || Index < 0 || _engine is null || !_engine.HasSource) return;
        if (ReferenceEquals(_engine, _ge) || _forceMp || DateTime.UtcNow < _graphDownUntil) return;
        Load(Index, IsActive, IsLive ? 0 : CurrentPosition());
    }

    /** Позиция с учётом ещё не выполненной перемотки (источник открывается). */
    private double CurrentPosition() =>
        _pendingSeek > 0 || _engine is null || _engine.State == EngineState.Opening ? Math.Max(_pendingSeek, Position) : _engine.Position;

    private void OnOpened(int gen)
    {
        if (gen != _gen || _engine is null) return;
        _failStreak = 0;
        var e = _engine;
        if (!IsLive && e.Duration > 0) Duration = e.Duration;
        if (_pendingSeek > 0 && !IsLive)
        {
            var p = Duration > 0 ? Math.Min(_pendingSeek, Duration) : _pendingSeek;
            e.Position = p;
            Position = p;
        }
        _pendingSeek = 0;
        if (_wantPlay) e.Play();
        else IsBuffering = false;
        UpdateTimeline();
    }

    /** Эфир оборвался: переподключение через 1, 2, 4, 8, 16 с, дальше — ошибка (20 с нормального эфира обнуляют счёт). */
    private async Task ReconnectLive(int gen)
    {
        if (++_liveRetries > 5)
        {
            _liveRetries = 0;
            Error = "Эфир недоступен — попробуйте позже";
            Stop();
            return;
        }
        IsBuffering = true;
        await Task.Delay(TimeSpan.FromSeconds(1 << (_liveRetries - 1)));
        if (gen != _gen || !IsLive || _shutDown) return;
        Load(Index, play: true);
    }

    private void OnEnded(int gen)
    {
        if (gen != _gen || _engine is null) return;
        if (IsLive) { _ = ReconnectLive(gen); return; }
        if (Repeat == RepeatMode.One)
        {
            _engine.Position = 0;
            Position = 0;
            _engine.Play();
            return;
        }
        if (Index < Queue.Count - 1) Load(Index + 1, play: true);
        else if (Repeat == RepeatMode.All && Queue.Count > 0) Load(0, play: true);
        else Stop();
    }

    private async Task OnFailed(int gen)
    {
        if (gen != _gen || Current is not { } t) return;
        var i = Index;
        var play = IsActive || _wantPlay;
        var pos = Position;
        if (ReferenceEquals(_engine, _ge) && !_forceMp)
        {
            // AudioGraph не открыл источник — тот же трек обычным плеером (без эквалайзера); граф — снова через минуту
            _forceMp = true;
            _graphDownUntil = DateTime.UtcNow.AddMinutes(1);
            Load(i, play, IsLive ? 0 : pos, retry: true);
            _retried = false;
            return;
        }
        if (IsLive)
        {
            Error = "Эфир недоступен — попробуйте позже";
            Stop();
            return;
        }
        if (!_retried)
        {
            // первая ошибка — скорее всего истекла подпись ссылки: берём свежую и повторяем с той же позиции
            _retried = true;
            try
            {
                var r = await Api.Instance.Tracks([t.Id]);
                if (gen != _gen) return;
                // за время запроса очередь могла сдвинуться: ищем трек заново
                i = CurrentIndexOf(t.Id);
                if (i < 0) return;
                if (r.Items.FirstOrDefault(x => x.Id == t.Id) is { StreamUri: not null } fresh)
                {
                    Queue[i] = fresh;
                    Load(i, play, pos, retry: true);
                    return;
                }
            }
            catch (Exception) { if (gen != _gen) return; }
            i = CurrentIndexOf(t.Id);
            if (i < 0) return;
        }
        Error = $"Не удалось воспроизвести «{t.Title}»";
        _failStreak++;
        var next = i + 1 < Queue.Count ? i + 1 : Repeat == RepeatMode.All ? 0 : -1;
        if (next < 0 || _failStreak >= Queue.Count) { Stop(); return; }
        Load(next, play);
    }

    /** Index, если на нём по-прежнему трек id, иначе -1. */
    private int CurrentIndexOf(long id) => Index >= 0 && Index < Queue.Count && Queue[Index].Id == id ? Index : -1;

    /** Остановка: пауза, позиция 0, трек остаётся выбранным. */
    private void Stop()
    {
        if (_engine is null) return;
        _wantPlay = false;
        _engine.Pause();
        StopWavePoll();
        if (_engine.HasSource && !IsLive) _engine.Position = 0;
        Position = 0;
        IsPlaying = false;
        IsBuffering = false;
        _tick?.Stop();
        UpdateTimeline();
        ScheduleSave();
    }

    private void OnStateChanged()
    {
        if (_engine is null) return;
        var st = _engine.State;
        switch (st)
        {
            case EngineState.Playing:
                IsPlaying = true;
                IsBuffering = false;
                _wantPlay = true;
                _tick?.Start();
                break;
            case EngineState.Paused:
                // позицию берём, только если играло (сразу после открытия она ещё не перемотана)
                if (_engine.HasSource && _tick?.IsRunning == true)
                {
                    if (!IsLive) Position = _engine.Position;
                    // играло и встало на паузу (наша пауза, система, пропало устройство) — играть больше не хотим
                    _wantPlay = false;
                }
                IsPlaying = false;
                IsBuffering = false;
                _tick?.Stop();
                break;
            case EngineState.Opening:
                IsBuffering = true;
                IsPlaying = _wantPlay;
                break;
            case EngineState.Buffering:
                IsBuffering = true;
                break;
            default:
                IsPlaying = false;
                IsBuffering = false;
                _tick?.Stop();
                break;
        }
        if (_smtc is not null)
        {
            _smtc.PlaybackStatus = st switch
            {
                EngineState.Playing => MediaPlaybackStatus.Playing,
                EngineState.Paused => MediaPlaybackStatus.Paused,
                EngineState.Opening or EngineState.Buffering => MediaPlaybackStatus.Changing,
                _ => Current is null ? MediaPlaybackStatus.Closed : MediaPlaybackStatus.Stopped,
            };
        }
        UpdateTimeline();
    }

    private void OnTick()
    {
        if (_engine is null || !_engine.HasSource) return;
        var p = _engine.Position;
        if (Math.Abs(p - Position) >= 0.05) Position = p;
        if (IsLive && _liveRetries > 0 && p > 20) _liveRetries = 0;
        // всплывающая панель Windows сама двигает ползунок; сверяем раз в 5 с, позицию в файл — раз в 15 с
        if (++_ticks % 20 == 0) UpdateTimeline();
        if (_ticks % 60 == 0) ScheduleSave();
    }

    private void OnSmtcButton(SystemMediaTransportControlsButton b)
    {
        switch (b)
        {
            case SystemMediaTransportControlsButton.Play: if (!IsActive) TogglePlay(); break;
            case SystemMediaTransportControlsButton.Pause: if (IsActive) TogglePlay(); break;
            case SystemMediaTransportControlsButton.Stop: if (IsActive) TogglePlay(); break;
            case SystemMediaTransportControlsButton.Next: Next(); break;
            case SystemMediaTransportControlsButton.Previous: Previous(); break;
        }
    }

    // ---------------------------------------------------------------- громкость

    /** Множитель нормализации: 10^(min(gain,6)/20), подъём не выше 0.99/peak; gain == null — 1. */
    internal static double NormalizeFactor(double? gain, double? peak)
    {
        if (gain is not double g) return 1;
        var m = Math.Pow(10, Math.Min(g, 6) / 20);
        if (m > 1 && peak is double p && p > 0) m = Math.Min(m, Math.Max(1, 0.99 / p));
        return m;
    }

    private void ApplyVolume()
    {
        if (_engine is null) return;
        var m = AppSettings.Current.Normalize && Current is { Live: false } t ? NormalizeFactor(t.Gain, t.Peak) : 1;
        _engine.SetVolume(Volume, IsMuted, m);
    }

    partial void OnVolumeChanged(double value)
    {
        ApplyVolume();
        if (Math.Abs(AppSettings.Current.Volume - value) < 0.0001) return;
        AppSettings.Current.Volume = value;
        if (_settingsTimer is null) return;
        _settingsTimer.Stop();
        _settingsTimer.Start();
    }

    partial void OnIsMutedChanged(bool value)
    {
        ApplyVolume();
        ScheduleSave();
    }

    private static void SaveSettings()
    {
        try { AppSettings.Current.Save(); } catch (Exception) { /* диск недоступен — не страшно */ }
    }

    // ---------------------------------------------------------------- очередь, порядок, переходы

    partial void OnShuffleChanged(bool value)
    {
        if (_smtc is not null) _smtc.ShuffleEnabled = value;
        if (value && !_quietShuffle) ShuffleAfterCurrent();
        ScheduleSave();
    }

    /** Перемешать треки после текущего (всю очередь, если ничего не выбрано). */
    private void ShuffleAfterCurrent()
    {
        var from = Index + 1;
        if (Queue.Count - from < 2) return;
        var tail = Queue.Skip(from).ToArray();
        Random.Shared.Shuffle(tail);
        Batch(() =>
        {
            for (var k = Queue.Count - 1; k >= from; k--) Queue.RemoveAt(k);
            foreach (var t in tail) Queue.Add(t);
        });
    }

    partial void OnRepeatChanged(RepeatMode value)
    {
        if (_smtc is not null)
            _smtc.AutoRepeatMode = value switch
            {
                RepeatMode.One => MediaPlaybackAutoRepeatMode.Track,
                RepeatMode.All => MediaPlaybackAutoRepeatMode.List,
                _ => MediaPlaybackAutoRepeatMode.None,
            };
        NotifyNav();
        ScheduleSave();
    }

    partial void OnIndexChanged(int value) => NotifyNav();

    partial void OnPositionChanged(double value)
    {
        if (HasPrevious != _lastHasPrevious) NotifyNav();
    }

    /** Пакетная правка очереди: CollectionChanged идут поштучно (для списка), работа плеера — один раз. */
    private void Batch(Action edit)
    {
        var outer = _batch;
        _batch = true;
        try { edit(); }
        finally { _batch = outer; }
        if (outer) return;
        NotifyNav();
        ScheduleSave();
    }

    private void OnQueueChanged(object? sender, NotifyCollectionChangedEventArgs e)
    {
        if (_batch) return;
        NotifyNav();
        ScheduleSave();
    }

    private void NotifyNav()
    {
        _lastHasPrevious = HasPrevious;
        OnPropertyChanged(nameof(HasPrevious));
        OnPropertyChanged(nameof(HasNext));
        if (_smtc is null) return;
        _smtc.IsPreviousEnabled = Current is not null;
        _smtc.IsNextEnabled = HasNext;
    }

    // ---------------------------------------------------------------- системные медиаэлементы

    private void UpdateSmtcDisplay(Track t)
    {
        if (_smtc is null) return;
        var d = _smtc.DisplayUpdater;
        d.Type = MediaPlaybackType.Music;
        d.MusicProperties.Title = t.Title;
        d.MusicProperties.Artist = t.Artist;
        d.MusicProperties.AlbumArtist = t.Artist;
        d.MusicProperties.AlbumTitle = t.Album;
        d.Thumbnail = t.CoverUri is { } c ? RandomAccessStreamReference.CreateFromUri(c) : null;
        d.Update();
        NotifyNav();
    }

    private void UpdateTimeline()
    {
        if (_smtc is null || Current is null) return;
        var end = TimeSpan.FromSeconds(Math.Max(Duration, 0));
        var pos = TimeSpan.FromSeconds(Math.Clamp(Position, 0, Duration > 0 ? Duration : double.MaxValue));
        _smtc.UpdateTimelineProperties(new SystemMediaTransportControlsTimelineProperties
        {
            StartTime = TimeSpan.Zero,
            MinSeekTime = TimeSpan.Zero,
            Position = pos,
            MaxSeekTime = end,
            EndTime = end,
        });
    }

    // ---------------------------------------------------------------- сохранение очереди

    /** queue.json: очередь целиком, текущий трек и позиция, режимы. */
    private sealed class SavedQueue
    {
        public List<Track> Tracks { get; set; } = [];
        public int Index { get; set; } = -1;
        public double Position { get; set; }
        public long SavedAt { get; set; }
        public bool Shuffle { get; set; }
        public RepeatMode Repeat { get; set; }
        public bool Muted { get; set; }
    }

    private void ScheduleSave()
    {
        if (_saveTimer is null || _shutDown) return;
        _saveTimer.Stop();
        _saveTimer.Start();
    }

    private void SaveQueue(bool sync)
    {
        // до конца восстановления пустая очередь затёрла бы сохранённую; без входа файл тоже не трогаем
        if (!_restored || IsLive || (Queue.Count == 0 && !Api.Instance.LoggedIn)) return;
        var seq = ++_saveSeq;
        string json;
        try
        {
            json = System.Text.Json.JsonSerializer.Serialize(new SavedQueue
            {
                Tracks = [.. Queue],
                Index = Index,
                Position = Math.Round(Position, 1),
                SavedAt = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
                Shuffle = Shuffle,
                Repeat = Repeat,
                Muted = IsMuted,
            }, Json.Options);
        }
        catch (Exception) { return; }
        if (sync) WriteQueue(json, seq);
        else _ = Task.Run(() => WriteQueue(json, seq));
    }

    private static void WriteQueue(string json, long seq)
    {
        lock (SaveGate)
        {
            if (seq <= _writtenSeq) return;
            _writtenSeq = seq;
            try
            {
                Directory.CreateDirectory(AppSettings.Dir);
                var tmp = QueuePath + ".tmp";
                File.WriteAllText(tmp, json);
                File.Move(tmp, QueuePath, overwrite: true);
            }
            catch (Exception) { /* не записалось — запишем при следующей смене */ }
        }
    }

    /** Восстановить очередь без автозапуска; старше 6 ч — обновить ссылки (недоступные треки выкинуть). */
    private async Task Restore()
    {
        try { await RestoreCore(); }
        catch (Exception) { /* испорченное сохранение — начинаем с пустой очереди */ }
        finally
        {
            _restored = true;
            // правки очереди за время восстановления не записывались — записать сейчас
            if (Queue.Count > 0) ScheduleSave();
        }
    }

    private async Task RestoreCore()
    {
        SavedQueue? s;
        try
        {
            if (!File.Exists(QueuePath)) return;
            s = System.Text.Json.JsonSerializer.Deserialize<SavedQueue>(await File.ReadAllTextAsync(QueuePath), Json.Options);
        }
        catch (Exception) { return; }
        if (s is null) return;

        _quietShuffle = true;
        Shuffle = s.Shuffle;
        _quietShuffle = false;
        Repeat = s.Repeat;
        IsMuted = s.Muted;

        if (!Api.Instance.LoggedIn || s.Tracks.Count == 0) return;
        var index = Math.Clamp(s.Index, 0, s.Tracks.Count - 1);
        var current = s.Tracks[index];
        List<Track> tracks;

        if (DateTimeOffset.UtcNow.ToUnixTimeSeconds() - s.SavedAt > RefreshAfter)
        {
            try
            {
                var fresh = new Dictionary<long, Track>();
                foreach (var chunk in s.Tracks.Select(t => t.Id).Distinct().Chunk(500))
                    foreach (var t in (await Api.Instance.Tracks(chunk)).Items)
                        fresh[t.Id] = t;
                // старые позиции → уцелевшие; текущий (или следующий за ним) остаётся выбранным
                tracks = [];
                var newIndex = -1;
                for (var k = 0; k < s.Tracks.Count; k++)
                {
                    if (!fresh.TryGetValue(s.Tracks[k].Id, out var f)) continue;
                    if (newIndex < 0 && k >= index) newIndex = tracks.Count;
                    tracks.Add(f);
                }
                if (tracks.Count == 0) return;
                if (newIndex < 0) newIndex = tracks.Count - 1;
                if (tracks[newIndex].Id != current.Id) s.Position = 0;
                index = newIndex;
            }
            catch (Exception)
            {
                tracks = FixLinks(s.Tracks);
            }
        }
        else tracks = FixLinks(s.Tracks);

        // пока восстанавливали, пользователь мог уже что-то включить
        if (Queue.Count > 0 || Current is not null || _shutDown) return;
        Batch(() => { foreach (var t in tracks) Queue.Add(t); });
        Load(index, play: false, Math.Max(0, s.Position));
    }

    private static List<Track> FixLinks(List<Track> tracks)
    {
        foreach (var t in tracks)
        {
            t.StreamUri = Api.Instance.Abs(t.Stream);
            t.CoverUri = Api.Instance.Abs(t.Cover);
        }
        return tracks;
    }
}
