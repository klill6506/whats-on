"""The home page's inline click handlers must survive show titles with quotes.

Regression for the broken Caught Up button and show cards: |tojson output (which uses
double quotes) was placed inside double-quoted onclick attributes, so the browser cut
every handler off at the title. Handlers now use single-quoted attributes, which is safe
because |tojson escapes ' as \\u0027."""
import re
from html.parser import HTMLParser

from jinja2.utils import htmlsafe_json_dumps

import main

TITLES = ['Say "Hello" & <Goodbye>', "Grey's Anatomy", "Bob's \"Burgers\""]


def _show(i, title, **kw):
    base = dict(id=i, title=title, service="Hulu", status="watching", priority=2,
                current_season=2, current_episode=3, rating=None, poster_url=None,
                air_day=None, has_new_season=False, latest_season=None)
    base.update(kw)
    return base


class _Onclicks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.handlers = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name == "onclick" and ("openEdit(" in value or "quickAction2(" in value):
                self.handlers.append(value)


def _render():
    template = main.templates.get_template("index.html")
    return template.render(
        priority_shows=[_show(1, TITLES[0], current_episode=99)],
        backup_shows=[_show(2, TITLES[1], priority=3, current_episode=99)],
        catching_up=[_show(3, TITLES[1])],
        between_seasons=[_show(4, TITLES[2], status="hiatus", has_new_season=True, latest_season=6)],
        recommendations=[],
    )


def test_click_handlers_keep_full_arguments_with_quoted_titles():
    parser = _Onclicks()
    parser.feed(_render())
    # 4 cards + Caught Up button + New Season button
    assert len(parser.handlers) == 6
    for handler in parser.handlers:
        assert handler.rstrip().endswith(")"), handler
        # the title argument must come through as a complete JSON string
        assert any(str(htmlsafe_json_dumps(t)) in handler for t in TITLES), handler


def test_page_script_has_no_line_breaks_inside_string_literals():
    html = _render()
    script = max(re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", html, re.S), key=len)
    for line in script.splitlines():
        code = line.split("//")[0] if not re.search(r"['\"].*//", line) else line
        # an odd number of single quotes on a line means a string literal ran onto the next line
        assert code.count("'") % 2 == 0, line
