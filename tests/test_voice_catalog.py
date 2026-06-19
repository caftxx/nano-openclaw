"""阿里云流式语音合成中文音色目录的结构与筛选校验。

目录由前端语音浮层「音色」下拉直接渲染，且断言「仅含中文音色」——防止后续维护
误把外语音色（英/日/韩/俄/西/葡/印尼/泰/越/马来等）或 *_ecmix 英中混合英文音色
混进来污染中文场景的下拉。
"""

from nano_openclaw.adapters.webui.voice_catalog import ALIYUN_TTS_VOICES


def test_catalog_non_empty_and_well_formed():
    assert isinstance(ALIYUN_TTS_VOICES, list)
    assert len(ALIYUN_TTS_VOICES) > 50
    for item in ALIYUN_TTS_VOICES:
        assert isinstance(item, dict)
        assert item.get("value")
        assert item.get("label")


def test_catalog_values_unique():
    values = [x["value"] for x in ALIYUN_TTS_VOICES]
    assert len(values) == len(set(values))


def test_catalog_contains_expected_chinese_voices():
    values = {x["value"] for x in ALIYUN_TTS_VOICES}
    assert "xiaoyun" in values        # 默认标准女声
    assert "xiaoxian" in values       # 默认音色（小仙·亲切女声）


def test_catalog_excludes_known_foreign_voices():
    values = {x["value"] for x in ALIYUN_TTS_VOICES}
    # 已知外语音色（英/日/韩/俄/西/葡/印尼/马来/泰/越/菲等）一律不应出现
    foreign = {
        "harry", "abby", "tomoka", "tomoya", "masha", "camila", "perla",
        "clara", "hanna", "waan", "indah", "farah", "tala", "tien",
        "becca", "Kyong", "ava", "lydia",
    }
    assert not (foreign & values)
    # *_ecmix 英中混合英文音色也排除
    assert not any(v.endswith("_ecmix") for v in values)
