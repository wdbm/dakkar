"""
Read image, audio and video metadata on demand for properties.
"""

from __future__ import annotations


def image_metadata(path: str) -> list[tuple[str, str]]:
    """
    Read image dimensions without decoding the full image.
    """

    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf, GLib
    except (ImportError, ValueError):
        return []

    try:
        image_format, width, height = GdkPixbuf.Pixbuf.get_file_info(path)
        if image_format is None:
            return []
        return [
            ("Dimensions", f"{width} × {height} pixels"),
            ("Image format", image_format.get_description() or image_format.get_name()),
        ]
    except (GLib.Error, OSError):
        return []


def media_metadata(uri: str) -> list[tuple[str, str]]:
    """
    Discover common audio and video properties through GStreamer.
    """

    try:
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstPbutils", "1.0")
        from gi.repository import GLib, Gst, GstPbutils
    except (ImportError, ValueError):
        return []

    try:
        Gst.init(None)
        discoverer = GstPbutils.Discoverer.new(5 * Gst.SECOND)
        information = discoverer.discover_uri(uri)
    except (GLib.Error, RuntimeError):
        return []

    values: list[tuple[str, str]] = []
    duration = information.get_duration()
    if duration not in (Gst.CLOCK_TIME_NONE, 0):
        seconds = duration // Gst.SECOND
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        values.append(("Duration", f"{hours:d}:{minutes:02d}:{seconds:02d}"))

    video_streams = information.get_video_streams()
    if video_streams:
        video = video_streams[0]
        values.append(("Dimensions", f"{video.get_width()} × {video.get_height()} pixels"))
        denominator = video.get_framerate_denom()
        if denominator:
            rate = video.get_framerate_num() / denominator
            values.append(("Frame rate", f"{rate:.3g} frames/s"))
        if video.get_bitrate():
            values.append(("Video bit rate", f"{video.get_bitrate() / 1000:.3g} kbit/s"))

    audio_streams = information.get_audio_streams()
    if audio_streams:
        audio = audio_streams[0]
        if audio.get_channels():
            values.append(("Audio channels", str(audio.get_channels())))
        if audio.get_sample_rate():
            values.append(("Sample rate", f"{audio.get_sample_rate()} Hz"))
        if audio.get_bitrate():
            values.append(("Audio bit rate", f"{audio.get_bitrate() / 1000:.3g} kbit/s"))
    return values


def read_media_metadata(path: str, uri: str, content_type: str) -> list[tuple[str, str]]:
    """
    Select a metadata reader from the MIME family.
    """

    if content_type.startswith("image/"):
        return image_metadata(path)
    if content_type.startswith(("audio/", "video/")):
        return media_metadata(uri)
    return []
