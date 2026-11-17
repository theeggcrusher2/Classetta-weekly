"""The chrome's own fonts, as theme picks them."""

from PyQt6.QtGui import QFont

from macrt.ui import theme


def test_the_chrome_draws_in_the_bundled_chicagoflf(qapp):
    # macrt/ui/fonts/ ships ChicagoFLF, so the system and application font
    # preferences find a Mac lookalike rather than a host fallback, and the
    # lookalike is drawn crisp, without antialiasing.
    assert theme.title_font().family() == "ChicagoFLF"
    assert theme.menu_bar_font().family() == "ChicagoFLF"
    assert theme.menu_item_font().family() == "ChicagoFLF"
    assert theme.body_font().family() == "ChicagoFLF"
    for f in (theme.title_font(), theme.body_font()):
        assert f.styleStrategy() & QFont.StyleStrategy.NoAntialias
