"""Read and write the audio arrangement subset of DAWproject.

DAWproject times may be beats or seconds. OTIO times here are always seconds
at a fixed rate of 1000 samples per second for sub-frame precision.
"""

from __future__ import annotations

import math
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from urllib.parse import unquote, urlparse
import wave
import xml.etree.ElementTree as ET
import zipfile

import opentimelineio as otio


RATE = 1000
KEY = "dawproject"
_AUTO_EXTRACT = object()


def _number(value, default=0.0):
    result = float(default if value is None else value)
    if not math.isfinite(result):
        raise ValueError("DAWproject time or tempo must be finite")
    return result


def _seconds(value, unit, tempo):
    value = _number(value)
    if unit == "seconds":
        return value
    if unit == "beats":
        return value * 60.0 / tempo
    raise ValueError("Unsupported DAWproject time unit: " + str(unit))


def _rt(seconds):
    return otio.opentime.RationalTime(seconds * RATE, RATE)


def _duration(item):
    return item.duration().to_seconds()


def _archive_path(name):
    """Only allow a relative, portable member name within the ZIP."""
    path = PurePosixPath(name.replace("\\", "/"))
    if not name or path.is_absolute() or ".." in path.parts or ":" in path.parts[0]:
        raise ValueError("Unsafe DAWproject media path: " + name)
    return str(path)


def _audio_leaf(element):
    """Find one audio object, including Bitwig's outer/inner Clip pattern."""
    direct = element.find("Audio")
    if direct is not None:
        return direct, None
    warps = element.find("Warps")
    if warps is not None and warps.find("Audio") is not None:
        return warps.find("Audio"), warps
    nested = element.findall("./Clips/Clip")
    if len(nested) == 1 and _number(nested[0].get("time")) == 0:
        return _audio_leaf(nested[0])
    return None, None


def _read_clip(element, unit, tempo, archive, archive_name, extract_dir):
    audio, warps = _audio_leaf(element)
    if audio is None:
        if element.find("Notes") is not None:
            return None
        raise ValueError("Unsupported DAWproject clip content (expected one Audio)")
    if element.get("duration") is None:
        raise ValueError("Audio clip has no duration")
    if element.get("loopStart") is not None:
        loop_end = _number(element.get("loopEnd"), -1)
        if abs(loop_end - _number(element.get("duration"))) > 1e-7:
            raise ValueError("Repeated DAWproject loops are not supported")
    duration = _seconds(element.get("duration"), unit, tempo)
    if duration <= 0:
        raise ValueError("Audio clip duration must be positive")
    source_start = _seconds(element.get("playStart"), element.get("contentTimeUnit", unit), tempo)
    speed = 1.0
    if warps is not None:
        points = warps.findall("Warp")
        if len(points) != 2:
            raise ValueError("Only two-point linear audio warps are supported")
        warp_unit = warps.get("timeUnit", unit)
        content_unit = warps.get("contentTimeUnit")
        t0, t1 = (_seconds(p.get("time"), warp_unit, tempo) for p in points)
        s0, s1 = (_seconds(p.get("contentTime"), content_unit, tempo) for p in points)
        if t1 <= t0 or s1 <= s0:
            raise ValueError("Invalid audio warp points")
        speed = (s1 - s0) / (t1 - t0)
        source_start = s0 + (source_start - t0) * speed
    file_node = audio.find("File")
    if file_node is None or not file_node.get("path"):
        raise ValueError("Audio element has no File path")
    media_path = _archive_path(file_node.get("path"))
    clip = otio.schema.Clip(name=element.get("name") or Path(media_path).stem)
    clip.source_range = otio.opentime.TimeRange(_rt(source_start), _rt(duration))
    clip.metadata[KEY] = {
        "archive": str(archive_name),
        "media_path": media_path,
        "media_duration": _number(audio.get("duration"), source_start + duration * speed),
        "channels": int(audio.get("channels", "2")),
        "sample_rate": int(audio.get("sampleRate", "48000")),
        "source_seconds_per_timeline_second": speed,
    }
    if extract_dir is not None:
        if media_path not in archive.namelist():
            raise FileNotFoundError("Media missing from DAWproject: " + media_path)
        output = Path(extract_dir) / Path(*PurePosixPath(media_path).parts)
        output.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(media_path) as source, output.open("wb") as target:
            shutil.copyfileobj(source, target)
        # Resolve currently treats percent escapes in file: URLs literally on
        # import (for example, "%20" instead of a space).  OTIO also permits
        # raw filesystem paths, which Resolve handles correctly.
        clip.media_reference = otio.schema.ExternalReference(target_url=output.resolve().as_posix())
    else:
        clip.media_reference = otio.schema.MissingReference()
    return clip


def _track_lanes(root):
    lanes = root.find("./Arrangement/Lanes")
    if lanes is None:
        return {}
    result = {}
    for lane in lanes.iter("Lanes"):
        track_id = lane.get("track")
        if track_id:
            result.setdefault(track_id, []).append(lane)
    return result


def read_from_file(filepath, extract_media_to=_AUTO_EXTRACT):
    """Read a .dawproject archive into an OTIO Timeline.

    Embedded media is extracted beside the archive by default so applications
    consuming the resulting OTIO receive playable ``ExternalReference`` URLs.
    Set ``extract_media_to`` to a directory to choose another location, or to
    ``None`` to keep media embedded and use ``MissingReference`` objects for a
    metadata-only read.
    """
    archive_name = Path(filepath).resolve()
    if extract_media_to is _AUTO_EXTRACT:
        extract_media_to = archive_name.with_name(archive_name.stem + "-media")
    with zipfile.ZipFile(archive_name) as archive:
        info = archive.getinfo("project.xml")
        if info.file_size > 32 * 1024 * 1024:
            raise ValueError("project.xml is too large")
        raw = archive.read(info)
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise ValueError("DTD and entities are not supported in project.xml")
        root = ET.fromstring(raw)
        if root.tag != "Project":
            raise ValueError("Archive does not contain a DAWproject Project")
        tempo_node = root.find("./Transport/Tempo")
        tempo = _number(tempo_node.get("value") if tempo_node is not None else None, 120)
        if tempo <= 0:
            raise ValueError("Tempo must be positive")
        if root.find("./Arrangement/TempoAutomation") is not None:
            raise ValueError("Tempo automation is not supported")
        lanes_by_track = _track_lanes(root)
        timeline = otio.schema.Timeline(name=archive_name.stem)
        signature = root.find("./Transport/TimeSignature")
        timeline.metadata[KEY] = {
            "tempo": tempo,
            "time_signature": [int(signature.get("numerator")), int(signature.get("denominator"))]
            if signature is not None else [4, 4],
        }
        structure = root.find("Structure")
        if structure is None:
            return timeline
        for source_track in structure.iter("Track"):
            channel = source_track.find("Channel")
            if channel is not None and channel.get("role") in ("master", "effect", "submix", "vca"):
                continue
            if "audio" not in (source_track.get("contentType") or "").split():
                continue
            track = otio.schema.Track(name=source_track.get("name") or "Audio", kind=otio.schema.TrackKind.Audio)
            track.metadata[KEY] = {"color": source_track.get("color", "")}
            found = []
            for lane in lanes_by_track.get(source_track.get("id"), []):
                unit = lane.get("timeUnit") or "beats"
                for clips in lane.findall("Clips"):
                    clip_unit = clips.get("timeUnit") or unit
                    for element in clips.findall("Clip"):
                        clip = _read_clip(element, clip_unit, tempo, archive, archive_name, extract_media_to)
                        if clip is not None:
                            found.append((_seconds(element.get("time"), clip_unit, tempo), clip))
            cursor = 0.0
            for start, clip in sorted(found, key=lambda item: item[0]):
                if start < cursor - 1e-6:
                    raise ValueError("Overlapping DAWproject clips cannot fit in one OTIO track")
                if start > cursor + 1e-6:
                    track.append(otio.schema.Gap(source_range=otio.opentime.TimeRange(_rt(0), _rt(start - cursor))))
                track.append(clip)
                cursor = start + _duration(clip)
            timeline.tracks.append(track)
        return timeline


def _media_file(clip, output, used_names):
    meta = clip.metadata.get(KEY, {})
    ref = clip.media_reference
    url = ref.target_url if isinstance(ref, otio.schema.ExternalReference) else None
    source = None
    if url:
        is_windows_path = os.name == "nt" and len(url) >= 3 and url[1] == ":" and url[2] in ("/", "\\")
        if is_windows_path:
            source = Path(url)
        else:
            parsed = urlparse(url)
            if parsed.scheme not in ("", "file") or parsed.netloc not in ("", "localhost"):
                raise ValueError("Only local media files can be embedded: " + url)
            source = Path(unquote(parsed.path)) if parsed.scheme == "file" else Path(url)
            if os.name == "nt" and parsed.scheme == "file" and source.as_posix().startswith("/"):
                source = Path(str(source)[1:])
        if not source.is_file():
            raise FileNotFoundError(source)
        media_name = source.name
        identity = ("file", str(source.resolve()))
    elif meta.get("archive") and meta.get("media_path"):
        member = _archive_path(meta["media_path"])
        media_name = PurePosixPath(member).name
        identity = ("archive", str(meta["archive"]), member)
    else:
        raise ValueError("Clip has no local media or DAWproject archive reference: " + clip.name)
    destination = "audio/" + media_name
    stem, suffix = os.path.splitext(media_name)
    counter = 2
    while destination in used_names and used_names[destination] != identity:
        destination = "audio/" + stem + "_" + str(counter) + suffix
        counter += 1
    if destination not in used_names:
        with output.open(destination, "w") as target:
            if source is not None:
                with source.open("rb") as stream:
                    shutil.copyfileobj(stream, target)
            else:
                with zipfile.ZipFile(meta["archive"]) as previous, previous.open(member) as stream:
                    shutil.copyfileobj(stream, target)
        used_names[destination] = identity
    if source is not None and source.suffix.lower() == ".wav":
        with wave.open(str(source)) as wav:
            return destination, wav.getnchannels(), wav.getframerate(), wav.getnframes() / wav.getframerate()
    required = ("channels", "sample_rate", "media_duration")
    if any(k not in meta for k in required):
        raise ValueError("Non-WAV media requires channels, sample_rate and media_duration in clip.metadata['dawproject']")
    return destination, int(meta["channels"]), int(meta["sample_rate"]), _number(meta["media_duration"])


def _fmt(value):
    return format(_number(value), ".12g")


def write_to_file(input_otio, filepath):
    """Write an OTIO Timeline as a DAWproject ZIP archive."""
    if not isinstance(input_otio, otio.schema.Timeline):
        raise TypeError("Expected an OTIO Timeline")
    metadata = input_otio.metadata.get(KEY, {})
    tempo = _number(metadata.get("tempo"), 120)
    if tempo <= 0:
        raise ValueError("Tempo must be positive")
    signature = metadata.get("time_signature", [4, 4])
    if len(signature) != 2 or any(int(n) <= 0 for n in signature):
        raise ValueError("Invalid time signature")
    root = ET.Element("Project", version="1.0")
    ET.SubElement(root, "Application", name="otio-dawproject-adapter", version="0.1.0")
    transport = ET.SubElement(root, "Transport")
    ET.SubElement(transport, "Tempo", id="tempo", name="Tempo", value=_fmt(tempo), unit="bpm")
    ET.SubElement(transport, "TimeSignature", id="signature", numerator=str(int(signature[0])), denominator=str(int(signature[1])))
    structure = ET.SubElement(root, "Structure")
    master = ET.SubElement(structure, "Track", id="master_track", name="Master", contentType="audio")
    ET.SubElement(master, "Channel", id="master_channel", role="master", audioChannels="2", solo="false")
    arrangement = ET.SubElement(root, "Arrangement", id="arrangement")
    lanes = ET.SubElement(arrangement, "Lanes", id="arrangement_lanes", timeUnit="seconds")
    ET.SubElement(root, "Scenes")
    destination = Path(filepath)
    temporary = tempfile.NamedTemporaryFile(prefix=".dawproject-", suffix=".tmp", dir=destination.parent, delete=False)
    temporary.close()
    try:
      with zipfile.ZipFile(temporary.name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        used_names = {}
        for index, track in enumerate(input_otio.tracks):
            if not isinstance(track, otio.schema.Track) or track.kind != otio.schema.TrackKind.Audio:
                raise ValueError("Only audio tracks are supported")
            track_id = "track_" + str(index)
            element = ET.Element("Track", id=track_id, name=track.name or "Audio", contentType="audio", loaded="true")
            color = track.metadata.get(KEY, {}).get("color")
            if color:
                element.set("color", color)
            ET.SubElement(element, "Channel", id="channel_" + str(index), role="regular", destination="master_channel", audioChannels="2", solo="false")
            structure.insert(index, element)
            lane = ET.SubElement(lanes, "Lanes", id="lane_" + str(index), track=track_id)
            clips_element = ET.SubElement(lane, "Clips", id="clips_" + str(index))
            cursor = 0.0
            for item in track:
                if isinstance(item, otio.schema.Gap):
                    cursor += _duration(item)
                    continue
                if not isinstance(item, otio.schema.Clip):
                    raise ValueError("Only clips and gaps are supported within audio tracks")
                if item.source_range is None:
                    raise ValueError("Audio clip requires source_range: " + item.name)
                duration = _duration(item)
                start = item.source_range.start_time.to_seconds()
                if duration <= 0 or start < 0:
                    raise ValueError("Audio clip has invalid source range")
                speed = _number(item.metadata.get(KEY, {}).get("source_seconds_per_timeline_second"), 1)
                if speed <= 0:
                    raise ValueError("Audio speed must be positive")
                path, channels, sample_rate, media_duration = _media_file(item, archive, used_names)
                if start + speed * duration > media_duration + 1e-5:
                    raise ValueError("Clip exceeds media duration: " + item.name)
                clip = ET.SubElement(clips_element, "Clip", name=item.name or Path(path).stem,
                                     time=_fmt(cursor), duration=_fmt(duration), playStart=_fmt(0), contentTimeUnit="seconds")
                warps = ET.SubElement(clip, "Warps", id="warps_" + str(index) + "_" + str(len(clips_element)),
                                      timeUnit="seconds", contentTimeUnit="seconds")
                audio = ET.SubElement(warps, "Audio", id="audio_" + str(index) + "_" + str(len(clips_element)),
                                      channels=str(channels), sampleRate=str(sample_rate), duration=_fmt(media_duration))
                ET.SubElement(audio, "File", path=path)
                ET.SubElement(warps, "Warp", time="0", contentTime=_fmt(start))
                ET.SubElement(warps, "Warp", time=_fmt(duration), contentTime=_fmt(start + speed * duration))
                cursor += duration
        archive.writestr("project.xml", ET.tostring(root, encoding="utf-8", xml_declaration=True))
        archive.writestr("metadata.xml", b'<?xml version="1.0" encoding="UTF-8"?><MetaData/>')
      os.replace(temporary.name, destination)
    finally:
      if os.path.exists(temporary.name):
          os.unlink(temporary.name)
    return filepath
