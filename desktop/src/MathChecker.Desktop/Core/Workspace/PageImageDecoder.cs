using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace MathChecker.Desktop.Core.Workspace;

/// <summary>One decoded page: its real pixel size and the frozen WPF source.</summary>
public sealed record DecodedPage(int PixelWidth, int PixelHeight, ImageSource? Source);

/// <summary>
/// Decodes page PNGs. Injectable so view-model tests never touch WPF imaging
/// (and never need an STA thread or a display).
/// </summary>
public interface IPageImageDecoder
{
    Task<DecodedPage> DecodeAsync(byte[] bytes, CancellationToken cancellationToken = default);
}

/// <summary>
/// Decodes on a worker thread and freezes the result, so the UI thread only
/// assigns an already-immutable bitmap. The backend's PNG is used at its
/// rendered resolution; the pane sizes it from pixels and the dpi it asked for.
/// </summary>
public sealed class WpfPageImageDecoder : IPageImageDecoder
{
    public Task<DecodedPage> DecodeAsync(byte[] bytes, CancellationToken cancellationToken = default) =>
        Task.Run(() =>
        {
            cancellationToken.ThrowIfCancellationRequested();
            using var stream = new MemoryStream(bytes, writable: false);
            var image = new BitmapImage();
            image.BeginInit();
            image.CacheOption = BitmapCacheOption.OnLoad;
            image.StreamSource = stream;
            image.EndInit();
            image.Freeze();
            return new DecodedPage(image.PixelWidth, image.PixelHeight, image);
        }, cancellationToken);
}
