"""The chrome's own fonts, as theme picks them."""

from PyQt6.QtGui import QFont, QFontDatabase

from macrt.ui import theme


def test_the_chrome_draws_in_the_bundled_chicagoflf(qapp):
    # macrt/ui/fonts/ ships ChicagoFLF, so the system font preferences
    # find a Mac lookalike rather than a host fallback, and the
    # lookalike is drawn crisp, without antialiasing.
    assert theme.title_font().family() == "ChicagoFLF"
    assert theme.menu_bar_font().family() == "ChicagoFLF"
    assert theme.menu_item_font().family() == "ChicagoFLF"
    for f in (theme.title_font(), theme.menu_bar_font(), theme.menu_item_font()):
        assert f.styleStrategy() & QFont.StyleStrategy.NoAntialias


def test_geneva_positions_never_fall_through_to_chicago(qapp):
    # Where the Mac uses Geneva, a Chicago lookalike must not stand in:
    # with no Geneva bundled, the Helvetica fallback is used instead.
    fam = theme.body_font().family()
    assert fam != "ChicagoFLF"
    assert fam != "Chicago"
    assert fam != "Charcoal"
    assert fam == ("Geneva" if "Geneva" in QFontDatabase.families()
                   else theme.FALLBACK_FONT[0])
