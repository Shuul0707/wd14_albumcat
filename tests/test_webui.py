"""The hand-checking web page: it must stay self-contained and in sync with the routes.

Two things can rot silently here: the page could start pulling a CDN script (it
is served on a LAN that may have no internet at all), and its ``fetch`` path
could drift away from the route the server actually registered.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from albumcat import server  # noqa: E402
from albumcat.webui import INDEX_HTML  # noqa: E402


def test_the_page_is_one_self_contained_document():
    assert INDEX_HTML.startswith("<!doctype html>")
    assert INDEX_HTML.rstrip().endswith("</html>")
    assert INDEX_HTML.count("<script>") == INDEX_HTML.count("</script>") == 1
    assert "<style>" in INDEX_HTML


def test_the_page_needs_no_network_beyond_this_server():
    body = INDEX_HTML
    for bad in ("http://", "https://", "//cdn", "<link rel=\"stylesheet\""):
        assert bad not in body, f"{bad} would need the internet"


def test_the_page_talks_to_the_endpoint_the_server_registered():
    assert "/classify" in INDEX_HTML
    assert '"/classify"' in inspect.getsource(server._Handler.do_POST)


def test_the_ui_endpoint_is_a_get_route():
    assert '"/ui"' in inspect.getsource(server._Handler.do_GET)


def test_the_page_can_send_the_shared_secret():
    # without this header a token-protected server answers 401 and the page is
    # useless, so the header name has to match the server's
    assert "X-Albumcat-Token" in INDEX_HTML
    assert 'self.headers.get("X-Albumcat-Token", "")' in inspect.getsource(server._Handler._authed)


def test_the_page_shows_the_losers_too():
    # the whole point: a runner-up is what makes a wrong call arguable
    assert "弃权" in INDEX_HTML
    assert "scores" in INDEX_HTML


def test_the_page_sends_the_original_size_for_the_size_prior():
    # the browser downscales before uploading, so it has to remember the real
    # dimensions itself; the classifier can only ever see a 448 px square
    assert "width: w0" in INDEX_HTML and "height: h0" in INDEX_HTML
    assert "size_prior" in INDEX_HTML
    source = inspect.getsource(server._Handler._handle_classify)
    assert 'width=int(obj.get("width") or 0)' in source, "the page's size never reaches the service"


def test_the_http_path_never_derives_the_size_from_the_uploaded_bytes():
    """A 448 px thumbnail must not be judged as a 448 px original."""
    classify = inspect.getsource(server.TaggingService.classify_bytes)
    assert "origin_size=(int(width), int(height))" in classify
    plan = inspect.getsource(server.TaggingService.plan_bytes)
    assert "origin_size=(int(width), int(height))" in plan


def test_the_page_scores_itself_against_the_servers_truth_map():
    # the accuracy panel is the difference between "looks right" and "68% right"
    assert '"/truth"' in inspect.getsource(server._Handler.do_GET)
    assert 'fetch("/truth"' in INDEX_HTML
    for word in ("准确度", "只看判错的", "标准答案"):
        assert word in INDEX_HTML


def test_truth_is_off_until_the_operator_points_at_a_folder():
    # scanning an arbitrary directory must never be implicit
    from albumcat import cli

    assert "self.truth_dir" in inspect.getsource(server.TaggingService.truth_view)
    assert "--truth-dir" in inspect.getsource(server.TaggingService.truth_view)
    assert '"--truth-dir"' in inspect.getsource(cli.build_parser)
    assert 'truth_dir=args.truth_dir or ""' in inspect.getsource(cli.cmd_serve)

