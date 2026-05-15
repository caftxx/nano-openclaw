"""Media extraction: picture / audio / video / file + richText image segments."""

from __future__ import annotations

from nano_openclaw.dingtalk.extract import extract_message


def test_picture_yields_one_image_attachment():
    msg = extract_message({
        "msgtype": "picture",
        "content": {"downloadCode": "dl-pic-1"},
        "conversationId": "c",
        "conversationType": "1",
    })
    assert len(msg.media) == 1
    item = msg.media[0]
    assert item.download_code == "dl-pic-1"
    assert item.mime == "image/jpeg"
    assert item.msgtype == "picture"
    assert item.name.endswith(".jpg")


def test_audio_yields_audio_attachment():
    msg = extract_message({
        "msgtype": "audio",
        "content": {"downloadCode": "dl-aud-1"},
        "conversationId": "c",
        "conversationType": "1",
    })
    assert len(msg.media) == 1
    assert msg.media[0].mime.startswith("audio/")
    assert msg.media[0].msgtype == "audio"


def test_file_uses_filename_when_provided():
    msg = extract_message({
        "msgtype": "file",
        "content": {"downloadCode": "dl-f", "fileName": "report.pdf"},
        "conversationId": "c",
        "conversationType": "1",
    })
    assert msg.media[0].name == "report.pdf"
    assert msg.media[0].mime == "application/octet-stream"


def test_rich_text_yields_one_attachment_per_image_segment():
    msg = extract_message({
        "msgtype": "richText",
        "content": {
            "richText": [
                {"text": "before"},
                {"downloadCode": "dl-rt-1"},
                {"text": "mid"},
                {"downloadCode": "dl-rt-2"},
            ],
        },
        "conversationId": "c",
        "conversationType": "1",
    })
    codes = [m.download_code for m in msg.media]
    assert codes == ["dl-rt-1", "dl-rt-2"]
    assert all(m.msgtype == "richText:image" for m in msg.media)
    # Text still extracted alongside media.
    assert msg.text == "before\nmid"


def test_text_only_message_has_no_media():
    msg = extract_message({
        "msgtype": "text",
        "text": {"content": "hi"},
        "conversationId": "c",
        "conversationType": "1",
    })
    assert msg.media == []
