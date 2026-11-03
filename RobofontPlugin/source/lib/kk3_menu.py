# encoding: utf-8
"""
Extensions ▸ Kinetikern3… — the Kinetikern3 window for the current font
(brought to the front when it is already open).
"""

from __future__ import division, print_function, unicode_literals

import os
import traceback

try:
    import kk3_args
    import kk3_window  # imports every module the windows use, while this script runs
    kk3_window.open_window(kk3_args.LIB)
    from PyObjCTools import AppHelper
    AppHelper.callAfter(kk3_args.keep_path)  # and the folder stays, once this script is done
except Exception:
    print(traceback.format_exc())
    import kk3_host
    kk3_host.show_output()
