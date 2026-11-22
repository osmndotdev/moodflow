from backend.analytics import clarity_id, inject_clarity

PAGE = "<html><head><title>MoodFlow</title></head><body></body></html>"


def test_clarity_id_normalizes_and_rejects_bad_values():
    assert clarity_id(" zhr0drdbro\n") == "zhr0drdbro"
    assert clarity_id("ZHR0DRDBRO") == "zhr0drdbro"
    assert clarity_id("") is None
    assert clarity_id(None) is None
    assert clarity_id('x"</script><script>alert(1)') is None


def test_inject_clarity_only_exposes_the_id_before_head_close():
    html = inject_clarity(PAGE, "zhr0drdbro")
    assert 'window.MOODFLOW_CLARITY_ID = "zhr0drdbro";' in html
    assert html.index("MOODFLOW_CLARITY_ID") < html.index("</head>")
    # the tag itself is loaded by the frontend only after cookie consent
    assert "clarity.ms" not in html


def test_inject_clarity_without_id_leaves_page_untouched():
    assert inject_clarity(PAGE, None) == PAGE
