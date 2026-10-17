"""Page view: layout, drawing, navigation, zoom and progressive loading (task 4.1)."""

import time
from pathlib import Path

import gi
import pytest

from sovander.views import render
from sovander.views.page_view import GAP, KEEP_PAGES, LOADING_ROW, MARGIN, PX_PER_PT, PasswordRequired

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk  # noqa: E402

try:
    import pymupdf as fitz
except ImportError:
    import fitz

pytestmark = pytest.mark.gui

RED_BOX = (72, 150, 272, 250)  # drawn on every page by conftest.build_pdf
A4_HEIGHT = round(842 * PX_PER_PT)
BIG_PDF = Path(__file__).resolve().parents[2] / "spike" / "corpus" / "pdf" / "pdf32000-2008.pdf"
CTRL = Gdk.ModifierType.CONTROL_MASK


def top_of(page):
    return MARGIN + page * (A4_HEIGHT + GAP)


def colour_at(view, image, page, x, y):
    spot = view.page_rect_to_widget(page, (x, y, x + 1, y + 1))
    return image.pixel(int(spot.get_x()), int(spot.get_y()))[:3]


def drawn_pixels(image, top, height):
    """Pixels in a band of rows that differ clearly from the band's left edge, i.e. something drawn there."""
    background = image.pixel(1, top)  # with alpha: the space below a page is transparent, the text is not
    rows = range(max(0, top), min(image.height, top + height))
    return sum(
        1
        for y in rows
        for x in range(0, image.width, 2)
        if max(abs(a - b) for a, b in zip(image.pixel(x, y), background, strict=True)) > 60
    )


def near(rgb, expected, tolerance=3):
    return all(abs(a - b) <= tolerance for a, b in zip(rgb, expected, strict=True))


class FakeEvent:
    """Stands in for the scroll controller and pinch gesture, which cannot be driven under Xvfb."""

    def __init__(self, state=0, centre=(0.0, 0.0)):
        self.state, self.centre = state, centre

    def get_current_event_state(self):
        return self.state

    def get_bounding_box_center(self):
        return True, *self.centre


def test_scrollbar_is_right_before_any_page_is_drawn(viewer, make_pdf):
    viewer.load(make_pdf(30))
    assert viewer.page_count == 30 and viewer.current_page == 0
    assert viewer.props.vadjustment.get_upper() == 2 * MARGIN + 30 * A4_HEIGHT + 29 * GAP
    assert not viewer.is_page_rendered(0)  # the main loop has not run yet


def test_first_page_is_drawn_with_its_content(viewer, make_pdf, wait, pixels):
    viewer.load(make_pdf(30))
    assert wait(lambda: viewer.is_page_rendered(0))
    image = pixels(viewer)
    assert near(colour_at(viewer, image, 0, 172, 200), (204, 25, 25))
    assert colour_at(viewer, image, 0, 500, 120) == (255, 255, 255)


def test_loads_from_a_path(viewer, make_pdf, tmp_path):
    path = tmp_path / "report.pdf"
    path.write_bytes(make_pdf(4))
    viewer.load(path)
    assert viewer.page_count == 4 and viewer.page_size(0) == (595, 842)


def test_go_to_scrolls_and_reports_the_page(viewer, make_pdf):
    viewer.load(make_pdf(30))
    seen = []
    viewer.connect("notify::current-page", lambda view, _spec: seen.append(view.current_page))
    viewer.go_to(10)
    assert viewer.current_page == 10 and seen == [10]
    assert viewer.page_rect_to_widget(10, (0, 0, 1, 1)).get_y() == pytest.approx(MARGIN)
    viewer.go_to(29)  # the last page cannot reach the top of the view but is still the current one
    assert viewer.current_page == 29
    viewer.go_to(999)
    assert viewer.current_page == 29
    viewer.go_to(-3)
    assert viewer.current_page == 0
    viewer.props.vadjustment.set_value(top_of(5) + 100)  # scrolling by hand
    assert viewer.current_page == 5


def test_zoom_keeps_the_point_under_the_anchor(viewer, make_pdf):
    viewer.load(make_pdf(30))
    viewer.go_to(3)
    page, before = viewer.widget_point_to_page(300, 200)
    viewer.set_zoom(2.0, (300, 200))
    assert viewer.zoom == 2.0 and viewer.sizing == "free"
    same_page, after = viewer.widget_point_to_page(300, 200)
    assert same_page == page
    assert after.x == pytest.approx(before.x, abs=1) and after.y == pytest.approx(before.y, abs=1)


def test_zoom_keys_step_through_levels(viewer, make_pdf):
    viewer.load(make_pdf(3))
    assert viewer.handle_key(Gdk.KEY_plus, CTRL) and viewer.zoom == 1.25
    assert viewer.handle_key(Gdk.KEY_minus, CTRL) and viewer.zoom == 1.0
    viewer.zoom_out()
    assert viewer.zoom == 0.8
    assert viewer.handle_key(Gdk.KEY_0, CTRL) and viewer.zoom == 1.0
    assert not viewer.handle_key(Gdk.KEY_a, CTRL)


def test_ctrl_scroll_and_pinch_zoom_around_the_pointer(viewer, make_pdf):
    viewer.load(make_pdf(30))
    viewer.go_to(2)
    viewer._pointer = (400.0, 300.0)  # where the motion controller last saw the pointer
    page, before = viewer.widget_point_to_page(400, 300)
    assert viewer._on_wheel(FakeEvent(), 0, -1) is False and viewer.zoom == 1.0  # plain wheel scrolls
    assert viewer._on_wheel(FakeEvent(CTRL), 0, -1) is True
    assert viewer.zoom == pytest.approx(1.1)
    same_page, after = viewer.widget_point_to_page(400, 300)
    assert same_page == page and after.y == pytest.approx(before.y, abs=1)

    viewer._on_pinch_begin(FakeEvent(centre=(200.0, 100.0)), None)
    page, before = viewer.widget_point_to_page(200, 100)
    viewer.pinch_zoom(2.0)
    assert viewer.zoom == pytest.approx(2.2)
    same_page, after = viewer.widget_point_to_page(200, 100)
    assert same_page == page and after.x == pytest.approx(before.x, abs=1)


def test_fit_width_and_fit_page_follow_the_window(viewer, window, make_pdf, wait):
    viewer.load(make_pdf(5))
    viewer.fit_width()
    assert viewer.sizing == "fit-width"
    assert viewer.page_rect_to_widget(0, (0, 0, 595, 842)).get_width() == pytest.approx(
        800 - 2 * MARGIN, abs=1
    )
    window.set_default_size(1000, 700)
    assert wait(lambda: viewer.get_width() == 1000)
    assert viewer.page_rect_to_widget(0, (0, 0, 595, 842)).get_width() == pytest.approx(
        1000 - 2 * MARGIN, abs=1
    )

    viewer.go_to(2)
    viewer.fit_page()
    box = viewer.page_rect_to_widget(2, (0, 0, 595, 842))
    assert box.get_height() == pytest.approx(viewer.get_height() - 2 * MARGIN, abs=1)
    assert box.get_y() == pytest.approx(MARGIN, abs=1) and viewer.current_page == 2
    viewer.zoom_in()
    assert viewer.sizing == "free"


def test_slides_mode_moves_one_page_per_key(viewer, make_pdf):
    viewer.load(make_pdf(6, size=(960, 540)))
    viewer.mode = "slides"
    viewer.fit_page()
    steps = [(Gdk.KEY_Right, 1), (Gdk.KEY_Down, 2), (Gdk.KEY_Page_Down, 3), (Gdk.KEY_space, 4)]
    steps += [(Gdk.KEY_Left, 3), (Gdk.KEY_Up, 2), (Gdk.KEY_Page_Up, 1), (Gdk.KEY_End, 5), (Gdk.KEY_Right, 5)]
    steps += [(Gdk.KEY_Home, 0), (Gdk.KEY_Left, 0)]
    for key, expected in steps:
        assert viewer.handle_key(key), key
        assert viewer.current_page == expected, Gdk.keyval_name(key)
    assert not viewer.handle_key(Gdk.KEY_a)


def test_continuous_mode_keys_scroll(viewer, make_pdf):
    viewer.load(make_pdf(30))
    adj = viewer.props.vadjustment
    assert viewer.handle_key(Gdk.KEY_Page_Down)
    assert adj.get_value() == pytest.approx(0.9 * viewer.get_height())
    assert viewer.handle_key(Gdk.KEY_Down)
    assert adj.get_value() == pytest.approx(0.9 * viewer.get_height() + 40)
    assert viewer.handle_key(Gdk.KEY_End)
    assert adj.get_value() == adj.get_upper() - adj.get_page_size() and viewer.current_page == 29
    assert viewer.handle_key(Gdk.KEY_Home) and adj.get_value() == 0


def test_geometry_helpers_round_trip(viewer, make_pdf):
    viewer.load(make_pdf(30))
    viewer.set_zoom(1.5)
    viewer.go_to(4)
    rect = viewer.page_rect_to_widget(4, fitz.Rect(100, 200, 300, 260))
    page, point = viewer.widget_point_to_page(rect.get_x() + 0.01, rect.get_y() + 0.01)
    assert page == 4 and point.x == pytest.approx(100, abs=0.5) and point.y == pytest.approx(200, abs=0.5)
    assert rect.get_width() == pytest.approx(200 * 1.5 * PX_PER_PT, abs=1)
    bottom = viewer.page_rect_to_widget(4, (0, 842, 595, 842))
    assert viewer.widget_point_to_page(bottom.get_x() + 10, bottom.get_y() + GAP / 2) is None


def test_replace_keeps_the_place_and_shows_loading_more(viewer, make_pdf, wait, pixels):
    full = make_pdf(30)
    first = fitz.open(stream=full, filetype="pdf")
    first.select([0])
    viewer.load(first.tobytes())
    viewer.set_loading_more(True)
    assert viewer.page_count == 1 and viewer.loading_more
    adj = viewer.props.vadjustment
    assert adj.get_upper() == 2 * MARGIN + A4_HEIGHT + LOADING_ROW
    adj.set_value(adj.get_upper() - adj.get_page_size())
    assert wait(lambda: viewer.is_page_rendered(0))
    page = viewer.page_rect_to_widget(0, (0, 0, 595, 842))
    row_top = int(page.get_y() + page.get_height() + MARGIN)
    assert drawn_pixels(pixels(viewer), row_top, LOADING_ROW - MARGIN) > 20  # the row's words, under the page

    adj.set_value(100)  # the red box stays in view
    assert wait(lambda: viewer.is_page_rendered(0))
    before = viewer.page_rect_to_widget(0, RED_BOX)
    viewer.replace(full)
    viewer.set_loading_more(False)
    assert viewer.page_count == 30 and not viewer.loading_more
    assert viewer.page_rect_to_widget(0, RED_BOX).get_y() == pytest.approx(before.get_y(), abs=1)
    # Until the full copy is drawn the old drawing stays on screen, so nothing flashes white
    assert near(colour_at(viewer, pixels(viewer), 0, 172, 200), (204, 25, 25))
    assert wait(lambda: viewer.is_page_rendered(0))


def test_textures_stay_near_the_view(viewer, make_pdf, wait):
    viewer.load(make_pdf(40))
    for page in range(0, 40, 3):
        viewer.go_to(page)
        assert wait(lambda page=page: viewer.is_page_rendered(page))
    drawn = [p for p in range(40) if viewer.is_page_rendered(p)]
    assert drawn and min(drawn) >= viewer.current_page - KEEP_PAGES
    assert 0 < viewer.texture_bytes <= render.TEXTURE_BUDGET


def test_over_budget_only_the_pages_on_screen_keep_textures(viewer, make_pdf, wait, monkeypatch):
    monkeypatch.setattr(render, "TEXTURE_BUDGET", 1)
    viewer.load(make_pdf(12))
    for page in (0, 4, 8):
        viewer.go_to(page)
        assert wait(lambda page=page: viewer.is_page_rendered(page))
    drawn = [p for p in range(12) if viewer.is_page_rendered(p)]
    assert set(drawn) <= {7, 8, 9}


def test_night_mode_darkens_paper_keeps_hue_and_photos(viewer, make_pdf, wait, pixels):
    viewer.load(make_pdf(2, photo=True))
    viewer.night = True
    assert wait(lambda: viewer.is_page_rendered(0))
    image = pixels(viewer)
    assert colour_at(viewer, image, 0, 500, 120) == (0, 0, 0)  # paper
    assert near(colour_at(viewer, image, 0, 272, 320), (127, 127, 255))  # dark-blue bar turns light blue
    assert near(colour_at(viewer, image, 0, 172, 200), (230, 51, 51))  # red stays red
    assert near(colour_at(viewer, image, 0, 172, 420), (40, 160, 60))  # the photo is untouched
    viewer.night = False
    assert wait(lambda: viewer.is_page_rendered(0))
    assert colour_at(viewer, pixels(viewer), 0, 500, 120) == (255, 255, 255)


def test_encrypted_pdf_needs_its_password(viewer, make_pdf):
    plain = fitz.open(stream=make_pdf(2), filetype="pdf")
    locked = plain.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="owl", owner_pw="sovander")
    with pytest.raises(PasswordRequired):
        viewer.load(locked)
    with pytest.raises(PasswordRequired):
        viewer.load(locked, password="cat")
    viewer.load(locked, password="owl")
    assert viewer.page_count == 2


@pytest.mark.skipif(not BIG_PDF.exists(), reason="spike corpus not downloaded")
def test_756_page_pdf_shows_page_one_fast(viewer, wait):
    start = time.perf_counter()
    viewer.load(BIG_PDF)
    laid_out = time.perf_counter() - start
    assert wait(lambda: viewer.is_page_rendered(0))
    drawn = time.perf_counter() - start
    print(f"\n756-page PDF: laid out in {laid_out:.3f} s, page 1 drawn after {drawn:.3f} s")
    assert viewer.page_count == 756
    assert drawn < 1.5  # NFR: first page of a 500-page PDF within 1.5 s
    viewer.go_to(700)
    assert wait(lambda: viewer.is_page_rendered(700))
