"""Golden wire-contract test - the JSON exchanged with the StandIn media bridge.

Locks the exact camelCase field names + type discriminators on both directions so
this plugin and the StandIn media bridge can't silently drift.
"""

from __future__ import annotations

import json

from hermes_teams_voice import protocol as p
from hermes_teams_voice import viseme_estimate as viz


def test_decode_inbound_session_start():
    m = p.decode(json.dumps({
        "type": "session.start", "callId": "c1", "threadId": "t1",
        "caller": {"aadId": "a", "displayName": "Dee", "tenantId": "tn"},
        "recordingStatus": "active", "direction": "inbound",
    }))
    assert isinstance(m, p.SessionStart)
    assert (m.call_id, m.thread_id, m.recording_status, m.direction) == ("c1", "t1", "active", "inbound")
    assert (m.caller.aad_id, m.caller.display_name, m.caller.tenant_id) == ("a", "Dee", "tn")


def test_decode_inbound_media_and_control():
    af = p.decode(json.dumps({"type": "audio.frame", "seq": 1, "timestampMs": 20, "payloadBase64": "AA", "speakerName": "Sara"}))
    assert (af.seq, af.timestamp_ms, af.payload_base64, af.speaker_name) == (1, 20, "AA", "Sara")
    vf = p.decode(json.dumps({"type": "video.frame", "source": "screenshare", "ts": 5, "width": 1,
                              "height": 2, "mime": "image/jpeg", "dataBase64": "ZZ",
                              "participantId": "pid", "participantName": "Bob"}))
    assert (vf.source, vf.data_base64, vf.participant_name) == ("screenshare", "ZZ", "Bob")
    assert p.decode(json.dumps({"type": "recording.status", "status": "active"})).status == "active"
    assert p.decode(json.dumps({"type": "participants", "count": 3})).count == 3
    assert p.decode(json.dumps({"type": "dtmf", "digit": "5"})).digit == "5"
    assert p.decode(json.dumps({"type": "assistant.say", "text": "bye now"})).text == "bye now"
    assert p.decode(json.dumps({"type": "ping", "ts": 99})).ts == 99
    assert p.decode(json.dumps({"type": "session.end", "reason": "call-ended"})).reason == "call-ended"


def test_encode_outbound_exact_keys():
    assert json.loads(p.encode(p.audio_frame(1, 20, "AA"))) == {
        "type": "audio.frame", "seq": 1, "timestampMs": 20, "payloadBase64": "AA"}
    assert json.loads(p.encode(p.expression("happy"))) == {"type": "expression", "emotion": "happy"}
    sm = json.loads(p.encode(p.speech_marks([{"tMs": 0, "visemeId": 2}], ts=0)))
    assert sm["type"] == "speech.marks" and sm["marks"][0] == {"tMs": 0, "visemeId": 2}
    di = json.loads(p.encode(p.display_image("ZZ", "image/png", duration_ms=5000, mode="overlay", caption="c")))
    assert di["type"] == "display.image"
    assert (di["dataBase64"], di["mime"], di["durationMs"], di["mode"], di["caption"]) == ("ZZ", "image/png", 5000, "overlay", "c")
    assert json.loads(p.encode(p.assistant_cancel(7))) == {"type": "assistant.cancel", "turnId": 7}
    assert json.loads(p.encode(p.pong(99))) == {"type": "pong", "ts": 99}


def test_blank_caller_fields_coerced_to_none():
    m = p.decode(json.dumps({"type": "session.start", "callId": "c", "threadId": "t", "caller": {"aadId": "  "}}))
    assert m.caller.aad_id is None  # blank-as-null guard (cross-caller bleed)


def test_minutes_docx_is_valid_openable(tmp_path):
    from hermes_teams_voice.meeting_docx import write_minutes_docx

    out = tmp_path / "m.docx"
    write_minutes_docx("Meeting minutes", "**Key Points**\n- shipped it\n**Decisions**\n- go", str(out))
    import zipfile

    assert zipfile.is_zipfile(out)
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml"} <= names
        body = z.read("word/document.xml").decode("utf-8")
        assert "Key Points" in body and "shipped it" in body  # content present + escaped


def test_arabic_visemes_are_shaped_not_neutral():
    # Arabic graphemes now map to real mouth shapes (not the neutral default).
    assert viz.viseme_for_char("و") == viz.VISEME_OO  # waw → round
    assert viz.viseme_for_char("ب") == viz.VISEME_MBP  # ba → closed
    assert viz.viseme_for_char("ف") == viz.VISEME_FV  # fa → lip-teeth
    marks = viz.estimate_visemes("توقف", 200)  # "stop" — should change shape, not flat
    assert len({m.viseme_id for m in marks}) >= 2
