# encoding: utf-8
"""
kk3_categories_window — tightness by category, and keeping the font's fit.

Every glyph has a category (kk3_groups.category_of: capitals, lowercase,
small caps, letters without case, figures, quotes, brackets, dashes, other
punctuation, symbols, marks and other). Each category gets a Looseness offset
on top of the main window's Looseness: quotes a little looser than the
letters, say. A glyph in a spacing group gets its group's offset as well.

Keep the font's fit: each kind of letter starts at the tightness of the
font's own spacing (the engine fits capitals, lowercase, small caps and
letters without case on their own; the rest follows the lowercase), and the
main Looseness and the offsets move from there. Stay close to the font's
spacing: every free sidebearing goes that far back to what it is in the font
before Kinetikern3 kerns (100 %: the font's spacing, only kerned).

Capitals to lowercase: a Looseness for the gap between a capital and a
lowercase letter (Ha, To, Wo), and one for a lowercase letter and a capital
(aH), on top of everything above. It works through their kerning, so a
capital's own sides, the capitals among themselves and the lowercase among
itself stay as they are.

Word space: as it is, in step with the n (its share of the n's advance),
or as designers of the font's kind set it (kk3_punctuation.WORD_SPACE). The
preview shows it; Apply sets it, and a no-break space as wide follows.

The window holds no state of its own: the main window keeps the settings
(saved with its others, and in Copy and Save Settings) and runs the preview.
"""

from __future__ import division, print_function, unicode_literals

import vanilla

import kk3_groups as kg
import kk3_punctuation as kpu
import kk3_host as host

OFFSET_MIN, OFFSET_MAX = kg.OFFSET_RANGE  # a category's Looseness offset, slider units
ROW_TOP, ROW_HEIGHT = 186, 30
WORD_SPACE_HEIGHT = 96  # the word space's line, pop-up and note under the rows
CASE_HEIGHT = 2 * ROW_HEIGHT + 50  # capitals to lowercase and back: a line, a note, two rows
CASE_ROWS = (("capsToLowercase", "Capitals to lowercase", "Ha To Wo"),
             ("lowercaseToCaps", "Lowercase to capitals", "aH oT"))


def _number(text):
    text = (text or "").strip().replace(",", ".").replace("−", "-").replace("%", "").strip()
    try:
        v = float(text)
    except ValueError:
        return None
    return v if v == v and abs(v) != float("inf") else None


class CategoriesWindow(object):
    def __init__(self, main):
        self.main = main
        self._closed = False
        self._rows = []  # [(key, slider, field)]
        self._case_rows = []  # [(key, slider, field, reset)]: capitals to lowercase and back
        self._build()

    def _title(self):
        return "Tightness by Category — %s" % host.font_title(self.main.font)

    def _build(self):
        main = self.main
        counts = main.category_counts()
        keys = [k for k in kg.CATEGORY_KEYS if counts is None or counts.get(k)]
        height = ROW_TOP + ROW_HEIGHT * len(keys) + CASE_HEIGHT + WORD_SPACE_HEIGHT + 50
        w = vanilla.Window((640, max(420, height)), self._title(), minSize=(560, 420))
        self.w = w
        w.keepFit = vanilla.CheckBox((14, 12, 300, 20), "Keep the font's fit", value=main.keep_fit,
                                     callback=self.keepFitChanged)
        w.keepFit.getNSButton().setToolTip_(
            "Each kind of letter starts at the tightness of the font's own spacing: capitals, lowercase, small "
            "caps and letters without case are fitted on their own, and the rest follows the lowercase. The main "
            "window's Looseness and the offsets below then move from there.")
        w.keepFitNote = vanilla.TextBox((34, 34, -14, 30), "", sizeStyle="small")
        w.stayCloseLabel = vanilla.TextBox((14, 74, 220, 17), "Stay close to the font's spacing", sizeStyle="small")
        w.stayClose = vanilla.Slider((236, 72, -112, 22), minValue=0.0, maxValue=100.0, value=main.stay_close,
                                     callback=self.stayCloseChanged)
        w.stayCloseField = vanilla.EditText((-102, 72, 52, 21), "", callback=self.stayCloseFieldChanged,
                                            continuous=False, sizeStyle="small")
        w.stayCloseUnit = vanilla.TextBox((-44, 76, 20, 14), "%", sizeStyle="small")
        w.stayCloseNote = vanilla.TextBox((14, 100, -14, 30),
                                          "Every sidebearing goes this far back to what it is in the font before "
                                          "Kinetikern3 kerns: 0 % is Kinetikern3's spacing, 100 % the font's own, "
                                          "only kerned.", sizeStyle="small")
        w.line = vanilla.HorizontalLine((14, 138, -14, 1))
        w.header = vanilla.TextBox((14, 148, -14, 30),
                                   "Looseness of each category, on top of the main window's (tight to the left, "
                                   "loose to the right); a glyph in a spacing group adds its group's.",
                                   sizeStyle="small")
        self._rows = []
        for n, key in enumerate(keys):
            y = ROW_TOP + ROW_HEIGHT * n
            label = dict(kg.CATEGORIES)[key]
            count = "" if counts is None else "{:,}".format(counts.get(key, 0))
            setattr(w, "label_" + key, vanilla.TextBox((14, y + 3, 170, 17), label, sizeStyle="small"))
            setattr(w, "count_" + key, vanilla.TextBox((186, y + 4, 50, 17), count, sizeStyle="mini", alignment="right"))
            slider = vanilla.Slider((248, y, -146, 22), minValue=OFFSET_MIN, maxValue=OFFSET_MAX,
                                    value=main.category_offsets.get(key, 0.0),
                                    callback=lambda sender, key=key: self.offsetChanged(key))
            field = vanilla.EditText((-136, y, 58, 21), "", continuous=False, sizeStyle="small",
                                     callback=lambda sender, key=key: self.offsetFieldChanged(key))
            reset = vanilla.Button((-70, y - 1, 56, 22), "0", sizeStyle="small",
                                   callback=lambda sender, key=key: self.resetOne(key))
            reset.getNSButton().setToolTip_("Back to 0: the category follows the main Looseness.")
            setattr(w, "slider_" + key, slider)
            setattr(w, "field_" + key, field)
            setattr(w, "reset_" + key, reset)
            self._rows.append((key, slider, field))
        # capitals to lowercase, and back: a Looseness for those pairs' gaps (kerning)
        y = ROW_TOP + ROW_HEIGHT * len(keys) + 6
        w.caseLine = vanilla.HorizontalLine((14, y, -14, 1))
        w.caseNote = vanilla.TextBox((14, y + 10, -14, 30),
                                     "How tight a capital sits to a lowercase letter, on top of everything above: "
                                     "their kerning moves, a capital's own sides stay.", sizeStyle="small")
        self._case_rows = []
        for n, (key, label, example) in enumerate(CASE_ROWS):
            yy = y + 44 + ROW_HEIGHT * n
            setattr(w, "caseLabel_" + key, vanilla.TextBox((14, yy + 3, 170, 17), label, sizeStyle="small"))
            setattr(w, "caseExample_" + key, vanilla.TextBox((186, yy + 4, 56, 17), example, sizeStyle="mini",
                                                             alignment="right"))
            slider = vanilla.Slider((248, yy, -146, 22), minValue=OFFSET_MIN, maxValue=OFFSET_MAX,
                                    value=main.case_offset(key), callback=lambda sender, key=key: self.caseChanged(key))
            field = vanilla.EditText((-136, yy, 58, 21), "", continuous=False, sizeStyle="small",
                                     callback=lambda sender, key=key: self.caseFieldChanged(key))
            reset = vanilla.Button((-70, yy - 1, 56, 22), "0", sizeStyle="small",
                                   callback=lambda sender, key=key: self.caseReset(key))
            reset.getNSButton().setToolTip_("Back to 0: as the rest of the spacing has it.")
            slider.getNSSlider().setToolTip_(
                "Tighter to the left, looser to the right: the gap moves by as much as this Looseness moves the "
                "letters' own gaps. It is kerning, held back by the collision floors.")
            setattr(w, "caseSlider_" + key, slider)
            setattr(w, "caseField_" + key, field)
            setattr(w, "caseReset_" + key, reset)
            self._case_rows.append((key, slider, field, reset))
        y += CASE_HEIGHT
        w.wordSpaceLine = vanilla.HorizontalLine((14, y, -14, 1))
        w.wordSpaceLabel = vanilla.TextBox((14, y + 15, 110, 17), "Word space", sizeStyle="small")
        w.wordSpace = vanilla.PopUpButton((124, y + 12, 200, 22), list(kpu.WORD_SPACE_LABELS),
                                          callback=self.wordSpaceChanged, sizeStyle="small")
        w.wordSpace.getNSPopUpButton().setToolTip_(
            "The word space has no outline for Kinetikern3 to read. As it is: left alone. In step with the n: it "
            "keeps its share of the n's advance as the n is spaced again. As designers set it: the share that "
            "designers of well-rated fonts of its kind give it. The preview shows it; Apply sets it, and a "
            "no-break space as wide follows it.")
        w.wordSpaceNote = vanilla.TextBox((14, y + 42, -14, 44), "", sizeStyle="small")
        w.resetAll = vanilla.Button((14, -34, 150, 22), "All categories to 0", callback=self.resetAll)
        w.bind("close", self.closed)
        w.open()
        self.refresh()

    # ------------------------------------------------------------ showing
    def refresh(self):
        """Shows the main window's settings and the fitted values of its last result."""
        if self._closed:
            return
        main = self.main
        w = self.w
        w.keepFit.set(main.keep_fit)
        w.keepFit.enable(main.font_fit_available())
        w.stayClose.set(main.stay_close)
        w.stayCloseField.set("%g" % round(main.stay_close, 1))
        for key, slider, field in self._rows:
            v = main.category_offsets.get(key, 0.0)
            slider.set(v)
            field.set("%+.2f" % v)
        w.keepFitNote.set(main.font_fit_note())
        w.wordSpace.set(kpu.WORD_SPACE_MODES.index(main.word_space))
        w.wordSpaceNote.set(self._word_space_text())
        able = main.case_pairs_available()
        for key, slider, field, reset in self._case_rows:
            v = main.case_offset(key)
            slider.set(v)
            field.set("%+.2f" % v)
            for c in (slider, field, reset):
                c.enable(able)

    def _word_space_text(self):
        note = self.main._word_space_note()
        if note is None:
            return ("The font has no blank word space or no n to measure it by: the word space stays as it is."
                    if self.main.snapshot is not None else "")
        return note[0].upper() + note[1:] + "."

    def result_ready(self):
        """The main window has a new result (its fitted values), or a word space setting."""
        if not self._closed:
            self.w.keepFitNote.set(self.main.font_fit_note())
            self.w.wordSpaceNote.set(self._word_space_text())

    # ----------------------------------------------------------- controls
    def keepFitChanged(self, sender):
        self.main.set_keep_fit(bool(self.w.keepFit.get()))
        self.refresh()

    def stayCloseChanged(self, sender):
        self.main.set_stay_close(round(float(self.w.stayClose.get())))
        self.refresh()

    def stayCloseFieldChanged(self, sender):
        v = _number(self.w.stayCloseField.get())
        if v is not None:
            self.main.set_stay_close(v)
        self.refresh()

    def _case_row(self, key):
        return next((r for r in self._case_rows if r[0] == key), None)

    def caseChanged(self, key):
        row = self._case_row(key)
        if row is not None:
            self.main.set_case_offset(key, round(float(row[1].get()), 2))
            self.refresh()

    def caseFieldChanged(self, key):
        row = self._case_row(key)
        if row is None:
            return
        v = _number(row[2].get())
        if v is not None:
            self.main.set_case_offset(key, v)
        self.refresh()

    def caseReset(self, key):
        self.main.set_case_offset(key, 0.0)
        self.refresh()

    def wordSpaceChanged(self, sender):
        self.main.set_word_space(kpu.WORD_SPACE_MODES[max(0, int(self.w.wordSpace.get()))])
        self.refresh()

    def _row(self, key):
        return next((r for r in self._rows if r[0] == key), None)

    def offsetChanged(self, key):
        row = self._row(key)
        if row is not None:
            self.main.set_category_offset(key, round(float(row[1].get()), 2))
            self.refresh()

    def offsetFieldChanged(self, key):
        row = self._row(key)
        if row is None:
            return
        v = _number(row[2].get())
        if v is not None:
            self.main.set_category_offset(key, v)
        self.refresh()

    def resetOne(self, key):
        self.main.set_category_offset(key, 0.0)
        self.refresh()

    def resetAll(self, sender):
        self.main.set_category_offsets({})
        self.refresh()

    # ---------------------------------------------------------------- life
    def front(self):
        self.w.getNSWindow().makeKeyAndOrderFront_(None)

    def closed(self, sender):
        self._closed = True
        self.main.categories_window_closed()

    def close(self):
        try:
            self.w.close()
        except Exception:
            pass
