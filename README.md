# otio-dawproject-adapter

Python adapter for converting DAWproject audio arrangements to and from OpenTimelineIO (OTIO).

## Install

```sh
pip install .
```

Installation registers the `dawproject` OTIO adapter for `.dawproject` files. You can then use the normal OTIO API:

```python
import opentimelineio as otio

timeline = otio.adapters.read_from_file("song.dawproject")
otio.adapters.write_to_file(timeline, "copy.dawproject")
```

Reading a DAWproject extracts its embedded media to a persistent
`song-media` directory beside the source archive. Clips receive raw absolute
filesystem paths, so OTIO consumers such as DaVinci Resolve can locate media
whose names contain spaces. This avoids a Resolve interoperability bug where
percent escapes in `file:` URLs (such as `%20`) are treated literally.
To choose another extraction directory, pass it explicitly:

```python
timeline = otio.adapters.read_from_file(
    "song.dawproject", extract_media_to="extracted-media"
)
```

For a metadata-only read without extraction, explicitly pass
`extract_media_to=None`. Clips then have `MissingReference` media references.
The original archive and member path are recorded in
`clip.metadata["dawproject"]`, so writing the timeline can still copy embedded
media from the original archive. Keep that archive available until writing is
complete. When writing a newly created OTIO timeline, clips need local
`ExternalReference` URLs. WAV headers provide the channel count, sample rate,
and media duration. For other audio formats set `channels`, `sample_rate`, and
`media_duration` in `clip.metadata["dawproject"]`.

## Supported conversion

- Audio tracks, clips, gaps, names, colors, constant tempo, and time signature.
- Beat or second positions on import; second positions on export.
- Source trims and two-point linear audio warps. Their source speed is kept in `clip.metadata["dawproject"]["source_seconds_per_timeline_second"]` and written back as warp points.
- Embedded audio files are copied into the output archive.

MIDI notes, automation, plug-ins, routing, markers, video, repeated loops, tempo changes, and complex warps are outside this adapter's current scope. Notes-only tracks are omitted. Unsupported audio clip structures and overlapping audio clips raise an error. OTIO stores a clip's visible duration but does not apply DAWproject's audio resampling itself; consumers must interpret the speed metadata or the exported warp points.

## Development

```sh
pip install -e ".[test]"
pytest
```
