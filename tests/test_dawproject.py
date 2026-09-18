import io
import wave
import zipfile
import xml.etree.ElementTree as ET

import pytest
import opentimelineio as otio

from otio_dawproject_adapter import dawproject


def wav_bytes(seconds=2):
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b"\0" * (int(seconds * 48000) * 4))
    return out.getvalue()


def test_read_nested_beat_clip_and_roundtrip(tmp_path):
    project = b'''<Project version="1.0"><Application name="Test" version="1"/>
      <Transport><Tempo value="120"/><TimeSignature numerator="4" denominator="4"/></Transport>
      <Structure><Track id="t1" name="Drums" contentType="audio"><Channel id="c1" role="regular"/></Track></Structure>
      <Arrangement id="arr"><Lanes id="ls" timeUnit="beats"><Lanes id="l1" track="t1"><Clips id="cs">
        <Clip time="2" duration="2" playStart="0" name="Hit"><Clips id="inner">
          <Clip time="0" duration="2"><Warps id="w" timeUnit="beats" contentTimeUnit="seconds">
            <Audio id="a" channels="2" sampleRate="48000" duration="2"><File path="audio/hit.wav"/></Audio>
            <Warp time="0" contentTime="0.25"/><Warp time="2" contentTime="1.75"/>
          </Warps></Clip></Clips></Clip>
      </Clips></Lanes></Lanes></Arrangement><Scenes/></Project>'''
    original = tmp_path / "original.dawproject"
    with zipfile.ZipFile(original, "w") as archive:
        archive.writestr("project.xml", project)
        archive.writestr("audio/hit.wav", wav_bytes())
    timeline = dawproject.read_from_file(original)
    assert len(timeline.tracks) == 1
    assert isinstance(timeline.tracks[0][0], otio.schema.Gap)
    clip = timeline.tracks[0][1]
    assert clip.source_range.start_time.to_seconds() == pytest.approx(.25)
    assert clip.source_range.duration.to_seconds() == pytest.approx(1)
    assert clip.metadata["dawproject"]["source_seconds_per_timeline_second"] == pytest.approx(1.5)
    output = tmp_path / "roundtrip.dawproject"
    dawproject.write_to_file(timeline, output)
    with zipfile.ZipFile(output) as archive:
        assert archive.read("audio/hit.wav") == wav_bytes()
        xml = ET.fromstring(archive.read("project.xml"))
        assert xml.find("./Arrangement/Lanes/Lanes/Clips/Clip").get("time") == "1"
    again = dawproject.read_from_file(output)
    assert again.tracks[0][1].source_range.start_time.to_seconds() == pytest.approx(.25)
    assert again.tracks[0][1].metadata["dawproject"]["source_seconds_per_timeline_second"] == pytest.approx(1.5)


def test_write_external_wav_and_extract(tmp_path):
    source = tmp_path / "sound.wav"
    source.write_bytes(wav_bytes())
    timeline = otio.schema.Timeline(name="Mix")
    track = otio.schema.Track(name="Audio 1", kind=otio.schema.TrackKind.Audio)
    timeline.tracks.append(track)
    track.append(otio.schema.Gap(source_range=otio.opentime.TimeRange(dawproject._rt(0), dawproject._rt(.5))))
    clip = otio.schema.Clip(name="Sound")
    clip.source_range = otio.opentime.TimeRange(dawproject._rt(.25), dawproject._rt(1))
    clip.media_reference = otio.schema.ExternalReference(target_url=source.as_uri())
    track.append(clip)
    output = tmp_path / "mix.dawproject"
    dawproject.write_to_file(timeline, output)
    restored = dawproject.read_from_file(output, extract_media_to=tmp_path / "extracted")
    assert restored.tracks[0][1].media_reference.target_url.startswith("file:")
    assert (tmp_path / "extracted" / "audio" / "sound.wav").read_bytes() == source.read_bytes()


def test_reject_unsupported_warps(tmp_path):
    project = b'''<Project version="1"><Application name="Test" version="1"/>
      <Structure><Track id="t" contentType="audio"/></Structure>
      <Arrangement><Lanes timeUnit="seconds"><Lanes track="t"><Clips>
      <Clip time="0" duration="1"><Warps contentTimeUnit="seconds">
      <Audio channels="2" sampleRate="48000" duration="1"><File path="audio/a.wav"/></Audio>
      <Warp time="0" contentTime="0"/><Warp time=".5" contentTime=".2"/><Warp time="1" contentTime="1"/>
      </Warps></Clip></Clips></Lanes></Lanes></Arrangement></Project>'''
    path = tmp_path / "bad.dawproject"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("project.xml", project)
    with pytest.raises(ValueError, match="two-point"):
        dawproject.read_from_file(path)
