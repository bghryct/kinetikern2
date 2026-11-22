# encoding: utf-8
"""
kk3_features_window — the OpenType features the sample text is set with.

Lists the font's substitution features (kk3_features.font_features), each
with a switch. Switching one on sets the sample text again with it, as an
Edit tab with that feature selected sets it (kk3_features.Shaper): both
panes show the glyphs it substitutes, the font as it is and Kinetikern3,
and the preview spaces and kerns those glyphs as it does the rest of the
sample. Small caps, old-style figures, case forms, fractions or a
stylistic set can so be looked at in running text, side by side with the
font's own spacing.

The window holds no state of its own: the main window keeps the features
switched on (saved with its settings, and in Copy and Save Settings) and
sets the sample.
"""

from __future__ import division, print_function, unicode_literals

import vanilla

import kk3_features as kf
import kk3_ui


class FeaturesWindow(object):
    def __init__(self, main):
        self.main = main
        self._closed = False
        self._setting = False  # the list is being filled: its edits are not the user's
        self._build()

    def _title(self):
        font = self.main.font
        return "OpenType Features — %s" % (font.familyName if font is not None else "")

    def _build(self):
        w = vanilla.Window((420, 520), self._title(), minSize=(360, 320))
        self.w = w
        w.note = vanilla.TextBox(
            (14, 10, -14, 58),
            "The sample text is set with the features switched on here, as an Edit tab sets it with them "
            "selected. Both panes show the glyphs they substitute, and Kinetikern3 spaces and kerns those "
            "glyphs too; Apply to Glyphs in sample text includes them.", sizeStyle="small")
        w.list = vanilla.List(
            (14, 74, -14, -100), [],
            columnDescriptions=[
                dict(title="", key="on", cell=vanilla.CheckBoxListCell(), width=24, editable=True),
                dict(title="Feature", key="name", editable=False)],
            editCallback=self.listEdited, allowsMultipleSelection=False, drawFocusRing=False,
            showColumnTitles=False, rowHeight=19)
        w.status = vanilla.TextBox((14, -92, -14, 44), "", sizeStyle="small")
        w.none = vanilla.Button((14, -40, 70, 22), "None", callback=self.noneClicked, sizeStyle="small")
        w.none.getNSButton().setToolTip_("Every feature off: the sample text as it is typed (the font's cmap and "
                                         "/name escapes).")
        w.defaults = vanilla.Button((92, -40, 112, 22), "Text defaults", callback=self.defaultsClicked,
                                    sizeStyle="small")
        w.defaults.getNSButton().setToolTip_(
            "Switches on what browsers and layout applications apply to running text by default (%s), "
            "those of them the font has; the others stay as they are." % ", ".join(kf.TEXT_DEFAULTS))
        w.compile = vanilla.Button((-136, -40, -14, 22), "Compile Again", callback=self.compileClicked,
                                   sizeStyle="small")
        w.compile.getNSButton().setToolTip_(
            "Compiles the font's features again (as Font Info ▸ Features ▸ Compile does) and sets the sample "
            "text with them: after you edit the feature code.")
        kk3_ui.on_close(w, self.closed)
        w.open()
        self.refresh()

    # ------------------------------------------------------------ showing
    def refresh(self):
        """Shows the font's features and which of them are on."""
        if self._closed:
            return
        main = self.main
        on = set(main.features)
        items = [dict(on=tag in on, tag=tag, name=kf.feature_name(tag, label))
                 for tag, label in main.font_feature_list()]
        self._setting = True
        try:
            self.w.list.set(items)
        finally:
            self._setting = False
        self.w.setTitle(self._title())
        self.result_ready()

    def result_ready(self):
        """The main window set the sample again (or found nothing to set it with)."""
        if not self._closed:
            self.w.status.set(self.main.features_note())

    # ----------------------------------------------------------- controls
    def listEdited(self, sender):
        if self._setting:
            return
        tags = [str(item["tag"]) for item in self.w.list.get() if bool(item["on"])]
        self.main.set_features(tags)
        self.result_ready()

    def noneClicked(self, sender):
        self.main.set_features([])
        self.refresh()

    def defaultsClicked(self, sender):
        have = set(tag for tag, _label in self.main.font_feature_list())
        tags = list(self.main.features) + [t for t in kf.TEXT_DEFAULTS if t in have and t not in self.main.features]
        self.main.set_features(tags)
        self.refresh()

    def compileClicked(self, sender):
        self.main.compile_features()
        self.refresh()

    # ---------------------------------------------------------------- life
    def front(self):
        self.w.getNSWindow().makeKeyAndOrderFront_(None)

    def closed(self, sender):
        self._closed = True
        self.main.features_window_closed()

    def close(self):
        kk3_ui.close(self.w)
