# encoding: utf-8
"""
Extensions ▸ Kinetikern2… — the Kinetikern2 window for the current font
(brought to the front when it is already open).
"""

from __future__ import division, print_function, unicode_literals

import os
import traceback

try:
    import kk2_args
    import kk2_window  # imports every module the windows use, while this script runs
    kk2_window.open_window(kk2_args.LIB)
    from PyObjCTools import AppHelper
    AppHelper.callAfter(kk2_args.keep_path)  # and the folder stays, once this script is done
except Exception:
    print(traceback.format_exc())
    import kk2_host
    kk2_host.show_output()
