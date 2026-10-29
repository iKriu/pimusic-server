using System.Collections.ObjectModel;
using Microsoft.UI.Xaml.Data;
using PiMusic.Data;
using Windows.Foundation;

namespace PiMusic.Views;

/** Список треков медиатеки с подгрузкой страниц по мере прокрутки (ListView сам вызывает LoadMoreItemsAsync). */
public sealed class TrackFeed(Func<int, Task<LibraryResult>> load) : ObservableCollection<Track>, ISupportIncrementalLoading
{
    private bool _busy;

    public bool HasMoreItems { get; private set; } = true;
    public int Total { get; private set; } = -1;
    public int LibraryTotal { get; private set; }
    public event Action? Loaded;
    public event Action<Exception>? Failed;

    public IAsyncOperation<LoadMoreItemsResult> LoadMoreItemsAsync(uint count) => More().AsAsyncOperation();

    private async Task<LoadMoreItemsResult> More()
    {
        if (_busy || !HasMoreItems) return new LoadMoreItemsResult { Count = 0 };
        _busy = true;
        try
        {
            var page = await load(Count);
            foreach (var t in page.Items) Add(t);
            Total = page.Total;
            LibraryTotal = page.LibraryTotal;
            HasMoreItems = page.Items.Count > 0 && Count < page.Total;
            Loaded?.Invoke();
            return new LoadMoreItemsResult { Count = (uint)page.Items.Count };
        }
        catch (Exception e)
        {
            HasMoreItems = false;
            Failed?.Invoke(e);
            return new LoadMoreItemsResult { Count = 0 };
        }
        finally { _busy = false; }
    }

    public void RemoveIds(IReadOnlyCollection<long> ids)
    {
        for (var i = Count - 1; i >= 0; i--)
            if (ids.Contains(this[i].Id)) RemoveAt(i);
        if (Total > 0) Total = Math.Max(0, Total - ids.Count);
        Loaded?.Invoke();
    }
}
