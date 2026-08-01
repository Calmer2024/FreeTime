from app.weibo import is_weibo_status_url, parse_weibo_status


def test_weibo_image_status_is_not_treated_as_failed_video() -> None:
    meta = {"idstr": "5326360685058013", "mblogid": "Rb3LeldOR",
        "text_raw": "反网暴法拟定版：人肉开盒最高判7年。",
        "user": {"idstr": "3898612300", "screen_name": "柳飘飘有点飘"},
        "pic_ids": ["first"], "pic_infos": {"first": {"original": {
            "url": "https://wx4.sinaimg.cn/orj1080/first.jpg", "width": 1080, "height": 764}}}}
    info = parse_weibo_status(meta, "https://weibo.com/3898612300/Rb3LeldOR")
    assert info["source_subtype"] == "weibo_image_post"
    assert info["title"].startswith("反网暴法拟定版")
    assert info["uploader"] == "柳飘飘有点飘"
    assert info["formats"] == []
    assert info["note_images"][0]["url"].endswith("first.jpg")


def test_tv_show_stays_on_generic_weibo_video_extractor() -> None:
    assert is_weibo_status_url("https://weibo.com/3898612300/Rb3LeldOR")
    assert not is_weibo_status_url(
        "https://weibo.com/tv/show/1034:5306469269307434"
    )
