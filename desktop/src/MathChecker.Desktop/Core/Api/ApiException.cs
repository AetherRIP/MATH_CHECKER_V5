namespace MathChecker.Desktop.Core.Api;

/// <summary>
/// A real failure reported by the backend (app/errors.py returns
/// <c>{"code": ..., "message": ...}</c>). The message is shown to the user
/// verbatim: the backend writes those sentences for the teacher.
/// </summary>
public sealed class ApiException : Exception
{
    public ApiException(int status, string? code, string message, string path, Exception? inner = null)
        : base(message, inner)
    {
        Status = status;
        Code = code;
        Path = path;
    }

    public int Status { get; }
    public string? Code { get; }
    public string Path { get; }

    public bool IsForbidden => Status == 403;
    public bool IsUnauthorized => Status == 401;
    public bool IsNotFound => Status == 404;

    public string Display =>
        string.IsNullOrWhiteSpace(Code) ? Message : $"{Message}  [{Code}]";
}

/// <summary>A file the backend produced, with the name it asked for.</summary>
public sealed record DownloadFile(string FileName, byte[] Bytes, string? ContentType)
{
    public string SizeText => Bytes.Length < 1024 * 1024
        ? $"{Bytes.Length / 1024d:0.#} KB"
        : $"{Bytes.Length / 1024d / 1024d:0.##} MB";
}
