from backend.analytics import clarity_id, inject_clarity

PAGE = "<html><head><title>MoodFlow</title></head><body></body></html>"


def test_clarity_id_normalizes_and_rejects_bad_values():
    assert clarity_id(" zhr0drdbro\n") == "zhr0drdbro"
    assert clarity_id("ZHR0DRDBRO") == "zhr0drdbro"
    assert clarity_id("") is None
    assert clarity_id(None) is None
    assert clarity_id('x"</script><script>alert(1)') is None


def test_inject_clarity_adds_tag_before_head_close():
    html = inject_clarity(PAGE, "zhr0drdbro")
    assert '"zhr0drdbro"' in html
    assert "https://www.clarity.ms/tag/" in html
    assert html.index("clarity.ms") < html.index("</head>")
    assert html.count("<script>") == 1
    assert "localhost" in html  # skips itself on local hosts


def test_inject_clarity_without_id_leaves_page_untouched():
    assert inject_clarity(PAGE, None) == PAGE
