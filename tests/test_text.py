from player.queue import LoopMode, TrackQueue
from player.text import escape_md, format_duration, format_queue, truncate
from player.track import Track


def mk(n, duration=None, requester="ana"):
    return Track(title=f"tema {n}", requester=requester, query=f"q{n}", duration=duration)


def test_format_duration():
    assert format_duration(None) == "?:??"
    assert format_duration(5) == "0:05"
    assert format_duration(185) == "3:05"
    assert format_duration(3725) == "1:02:05"


def test_truncate():
    assert truncate("abc", 5) == "abc"
    assert truncate("abcdef", 5) == "abcd…"
    assert len(truncate("x" * 500, 80)) == 80


def test_escape_md_neutralizes_markdown_and_masked_links():
    assert escape_md("*hola* [x](http://a)") == r"\*hola\* \[x\](http://a)"


def test_empty_queue_text():
    text, pages = format_queue(TrackQueue())
    assert "No hay más temas" in text and pages == 1


def test_queue_pagination_and_numbering():
    q = TrackQueue()
    q.add_many([mk(i, duration=60) for i in range(1, 26)])
    q.advance()  # tema 1 pasa a sonar; quedan 24
    text, pages = format_queue(q, page=2)
    assert pages == 3
    assert "`11.` tema 12" in text and "`20.` tema 21" in text
    assert "`10.`" not in text and "`21.`" not in text
    assert "Página 2/3" in text and "24 en cola" in text


def test_page_is_clamped():
    q = TrackQueue()
    q.add_many([mk(i) for i in range(1, 4)])
    text, pages = format_queue(q, page=99)
    assert pages == 1 and "Página 1/1" in text


def test_footer_shows_loop_and_partial_total():
    q = TrackQueue()
    q.loop = LoopMode.QUEUE
    q.add_many([mk(1, duration=120), mk(2, duration=None)])
    text, _ = format_queue(q)
    assert "loop: cola" in text
    assert "2:00+ en total" in text  # el "+" avisa que falta la duración de alguno


def test_every_line_stays_well_under_discord_limit():
    q = TrackQueue()
    q.add_many([Track(title="x" * 500, requester="y" * 100, query="q") for _ in range(10)])
    text, _ = format_queue(q)
    assert len(text) < 2000
