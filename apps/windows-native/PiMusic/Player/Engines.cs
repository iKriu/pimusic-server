using System.Runtime.InteropServices;
using Windows.Media.Audio;
using Windows.Media.Core;
using Windows.Media.Devices;
using Windows.Media.Playback;
using Windows.Media.Render;

namespace PiMusic.Player;

internal enum EngineState { Closed, Opening, Buffering, Playing, Paused }

/**
 * Движок воспроизведения одного источника. События приходят из фоновых потоков; gen — номер загрузки,
 * по нему PlayerService отбрасывает события прежнего источника.
 */
internal interface IAudioEngine : IDisposable
{
    event Action<int>? Opened;
    event Action<int>? Ended;
    event Action<int>? Failed;
    event Action<IAudioEngine>? StateChanged;

    bool HasSource { get; }
    EngineState State { get; }
    /** секунды */
    double Position { get; set; }
    /** секунды (0 — неизвестна или прямой эфир) */
    double Duration { get; }

    void Open(Uri uri, int gen);
    void Close();
    void Play();
    void Pause();
    /** громкость пользователя 0…1 и множитель нормализации трека */
    void SetVolume(double volume, bool muted, double trackGain);
}

/** Обычный MediaPlayer (без обработки звука). Его SystemMediaTransportControls PlayerService использует всегда. */
internal sealed class MediaPlayerEngine : IAudioEngine
{
    private const string GenKey = "pimusic.gen";
    private MediaSource? _src;

    public MediaPlayer Player { get; }
    public event Action<int>? Opened;
    public event Action<int>? Ended;
    public event Action<int>? Failed;
    public event Action<IAudioEngine>? StateChanged;

    public MediaPlayerEngine(MediaPlayer player)
    {
        Player = player;
        Player.MediaOpened += (sp, _) => Opened?.Invoke(GenOf(sp));
        Player.MediaEnded += (sp, _) => Ended?.Invoke(GenOf(sp));
        Player.MediaFailed += (sp, _) => Failed?.Invoke(GenOf(sp));
        Player.PlaybackSession.PlaybackStateChanged += (_, _) => StateChanged?.Invoke(this);
    }

    /** Номер загрузки источника, от которого пришло событие (-1 — чужой или уже снятый). */
    private static int GenOf(MediaPlayer sp)
    {
        try { return sp.Source is MediaSource ms && ms.CustomProperties.TryGetValue(GenKey, out var v) && v is int g ? g : -1; }
        catch (Exception) { return -1; }
    }

    public bool HasSource => _src is not null;

    public EngineState State => _src is null ? EngineState.Closed : Player.PlaybackSession.PlaybackState switch
    {
        MediaPlaybackState.Playing => EngineState.Playing,
        MediaPlaybackState.Paused => EngineState.Paused,
        MediaPlaybackState.Opening => EngineState.Opening,
        MediaPlaybackState.Buffering => EngineState.Buffering,
        _ => EngineState.Closed,
    };

    public double Position
    {
        get => Player.PlaybackSession.Position.TotalSeconds;
        set { if (_src is not null && Player.PlaybackSession.CanSeek) Player.PlaybackSession.Position = TimeSpan.FromSeconds(value); }
    }

    public double Duration => Player.PlaybackSession.NaturalDuration.TotalSeconds;

    public void Open(Uri uri, int gen)
    {
        var old = _src;
        _src = MediaSource.CreateFromUri(uri);
        _src.CustomProperties[GenKey] = gen;
        Player.Source = _src;
        old?.Dispose();
    }

    public void Close()
    {
        try { Player.Pause(); } catch (Exception) { }
        Player.Source = null;
        _src?.Dispose();
        _src = null;
    }

    public void Play() => Player.Play();
    public void Pause() => Player.Pause();

    public void SetVolume(double volume, bool muted, double trackGain)
    {
        Player.Volume = Math.Clamp(volume * trackGain, 0, 1);
        Player.IsMuted = muted;
    }

    public void Dispose() => Close();
}

/**
 * AudioGraph с эквалайзером: MediaSourceAudioInputNode → AudioFrameOutputNode → EqDsp (в QuantumStarted) →
 * AudioFrameInputNode → устройство вывода. Громкость — усиление входного узла; нормализация и лимитер — в EqDsp.
 * Смена устройства вывода по умолчанию или его потеря — DeviceLost (PlayerService пересоздаёт движок).
 */
internal sealed class GraphEngine : IAudioEngine
{
    private readonly EqDsp _dsp;
    private readonly Lock _gate = new();
    private Task<bool>? _ready;
    private AudioGraph? _graph;
    private AudioDeviceOutputNode? _out;
    private AudioFrameInputNode? _in;
    private AudioFrameOutputNode? _tap;
    private MediaSourceAudioInputNode? _node;
    private MediaSource? _ms;
    private volatile int _gen = -1;
    private int _channels = 2;
    private volatile EngineState _state = EngineState.Closed;
    private double _volume = 0.8;
    private bool _muted;
    private volatile bool _disposed;

    public event Action<int>? Opened;
    public event Action<int>? Ended;
    public event Action<int>? Failed;
    public event Action<IAudioEngine>? StateChanged;
    /** устройство вывода сменилось или пропало — граф нужно пересоздать */
    public event Action? DeviceLost;

    public GraphEngine(EqDsp dsp)
    {
        _dsp = dsp;
        MediaDevice.DefaultAudioRenderDeviceChanged += OnDefaultDeviceChanged;
    }

    private void OnDefaultDeviceChanged(object? sender, DefaultAudioRenderDeviceChangedEventArgs e)
    {
        if (e.Role == AudioDeviceRole.Default && _graph is not null && !_disposed) DeviceLost?.Invoke();
    }

    private Task<bool> EnsureGraph()
    {
        lock (_gate) return _ready ??= Build();
    }

    private async Task<bool> Build()
    {
        try
        {
            var g = await AudioGraph.CreateAsync(new AudioGraphSettings(AudioRenderCategory.Media));
            if (g.Status != AudioGraphCreationStatus.Success) return Retry();
            var graph = g.Graph;
            var o = await graph.CreateDeviceOutputNodeAsync();
            if (o.Status != AudioDeviceNodeCreationStatus.Success) { graph.Dispose(); return Retry(); }
            var enc = graph.EncodingProperties;
            _channels = (int)Math.Max(1, enc.ChannelCount);
            _dsp.SampleRate = (int)enc.SampleRate;
            _tap = graph.CreateFrameOutputNode(enc);
            _in = graph.CreateFrameInputNode(enc);
            _in.AddOutgoingConnection(o.DeviceOutputNode);
            _in.OutgoingGain = _muted ? 0 : _volume;
            _out = o.DeviceOutputNode;
            graph.QuantumStarted += OnQuantum;
            graph.UnrecoverableErrorOccurred += (_, _) => { if (!_disposed) DeviceLost?.Invoke(); };
            _graph = graph;
            if (_disposed) { Teardown(); return false; }
            return true;
        }
        catch (Exception) { return Retry(); }
    }

    /** Не собрался — в следующий раз попробовать заново (устройство могло появиться). */
    private bool Retry()
    {
        lock (_gate) _ready = null;
        return false;
    }

    public bool HasSource => _node is not null || _state == EngineState.Opening;
    public EngineState State => _state;

    public double Position
    {
        get { try { return _node?.Position.TotalSeconds ?? 0; } catch (Exception) { return 0; } }
        set { try { _node?.Seek(TimeSpan.FromSeconds(Math.Max(0, value))); } catch (Exception) { } }
    }

    public double Duration { get { try { return _node?.Duration.TotalSeconds ?? 0; } catch (Exception) { return 0; } } }

    public void Open(Uri uri, int gen)
    {
        _gen = gen;
        try { _graph?.Stop(); } catch (Exception) { }
        DropNode();
        SetState(EngineState.Opening);
        _ = OpenAsync(uri, gen);
    }

    private async Task OpenAsync(Uri uri, int gen)
    {
        try
        {
            if (!await EnsureGraph() || _graph is not { } graph)
            {
                if (gen == _gen) { SetState(EngineState.Closed); Failed?.Invoke(gen); }
                return;
            }
            var ms = MediaSource.CreateFromUri(uri);
            var r = await graph.CreateMediaSourceAudioInputNodeAsync(ms);
            if (gen != _gen || _disposed)
            {
                r.Node?.Dispose();
                ms.Dispose();
                return;
            }
            if (r.Status != MediaSourceAudioInputNodeCreationStatus.Success)
            {
                ms.Dispose();
                SetState(EngineState.Closed);
                Failed?.Invoke(gen);
                return;
            }
            _ms = ms;
            _node = r.Node;
            _node.MediaSourceCompleted += (sender, _) =>
            {
                if (gen != _gen) return;
                // источник кончился заметно раньше длительности — скорее всего, оборвалась сеть: это ошибка (повтор)
                double pos = 0, dur = 0;
                try { pos = sender.Position.TotalSeconds; dur = sender.Duration.TotalSeconds; } catch (Exception) { }
                if (dur > 10 && pos < dur - 5) Failed?.Invoke(gen);
                else Ended?.Invoke(gen);
            };
            _node.AddOutgoingConnection(_tap);
            _dsp.Reset();
            SetState(EngineState.Paused);
            Opened?.Invoke(gen);
        }
        catch (Exception)
        {
            if (gen != _gen) return;
            SetState(EngineState.Closed);
            Failed?.Invoke(gen);
        }
    }

    private void DropNode()
    {
        var node = _node;
        var ms = _ms;
        _node = null;
        _ms = null;
        if (node is not null)
        {
            try { node.RemoveOutgoingConnection(_tap); } catch (Exception) { }
            try { node.Dispose(); } catch (Exception) { }
        }
        try { ms?.Dispose(); } catch (Exception) { }
    }

    public void Close()
    {
        _gen = -1;
        try { _graph?.Stop(); } catch (Exception) { }
        DropNode();
        SetState(EngineState.Closed);
    }

    public void Play()
    {
        if (_graph is null || _node is null) return;
        _graph.Start();
        SetState(EngineState.Playing);
    }

    public void Pause()
    {
        if (_graph is null) return;
        _graph.Stop();
        if (_node is not null) SetState(EngineState.Paused);
    }

    public void SetVolume(double volume, bool muted, double trackGain)
    {
        _volume = Math.Clamp(volume, 0, 1);
        _muted = muted;
        if (_in is not null) _in.OutgoingGain = muted ? 0 : _volume;
        _dsp.SetTrackGain(trackGain);
    }

    private void SetState(EngineState s)
    {
        if (_state == s) return;
        _state = s;
        StateChanged?.Invoke(this);
    }

    /** Поток звука: забрать накопленное из узла-источника, обработать, отдать на вывод. */
    private unsafe void OnQuantum(AudioGraph sender, object args)
    {
        var tap = _tap;
        var input = _in;
        if (tap is null || input is null || _disposed) return;
        // исключение в потоке звука уронило бы процесс, а граф могут разобрать из потока интерфейса
        try
        {
            using var frame = tap.GetFrame();
            using (var buf = frame.LockBuffer(Windows.Media.AudioBufferAccessMode.ReadWrite))
            {
                if (buf.Length == 0) return;
                using var reference = buf.CreateReference();
                if (GetBytes(reference, out var data))
                    _dsp.Process((float*)data, (int)(buf.Length / sizeof(float) / (uint)_channels), _channels);
            }
            input.AddFrame(frame);
        }
        catch (Exception) { }
    }

    private static readonly Guid ByteAccessIid = new("5B0D3235-4DBA-4D44-865E-8F1D0E4FD04D");

    /** IMemoryBufferByteAccess::GetBuffer напрямую через vtable (без COM-обёрток .NET). */
    private static unsafe bool GetBytes(Windows.Foundation.IMemoryBufferReference reference, out byte* data)
    {
        data = null;
        var unk = ((WinRT.IWinRTObject)reference).NativeObject.ThisPtr;
        var iid = ByteAccessIid;
        if (Marshal.QueryInterface(unk, in iid, out var p) != 0 || p == IntPtr.Zero) return false;
        try
        {
            byte* d;
            uint c;
            var getBuffer = (delegate* unmanaged[Stdcall]<IntPtr, byte**, uint*, int>)(*(void***)p)[3];
            if (getBuffer(p, &d, &c) != 0) return false;
            data = d;
            return true;
        }
        finally { Marshal.Release(p); }
    }

    private void Teardown()
    {
        if (_graph is not null) _graph.QuantumStarted -= OnQuantum;
        try { _graph?.Stop(); } catch (Exception) { }
        DropNode();
        try
        {
            _in?.Dispose();
            _tap?.Dispose();
            _out?.Dispose();
            _graph?.Dispose();
        }
        catch (Exception) { }
        _in = null;
        _tap = null;
        _out = null;
        _graph = null;
    }

    public void Dispose()
    {
        _disposed = true;
        _gen = -1;
        MediaDevice.DefaultAudioRenderDeviceChanged -= OnDefaultDeviceChanged;
        try { _graph?.Stop(); } catch (Exception) { }
        Teardown();
    }
}
