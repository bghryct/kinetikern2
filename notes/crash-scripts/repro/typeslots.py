import ctypes, gc
from Foundation import NSObject, NSMutableArray
from AppKit import NSTextAttachment, NSTextAttachmentCell
class KK2ReproCell(NSTextAttachmentCell):
    pass
def slots(t):
    base = id(t)
    rd = lambda off: ctypes.c_void_p.from_address(base + off).value
    return {"tp_dealloc": hex(rd(48) or 0), "tp_traverse": hex(rd(184) or 0), "tp_clear": hex(rd(192) or 0),
            "flags": hex(ctypes.c_ulong.from_address(base + 168).value)}
for obj in (NSMutableArray.alloc().init(), NSTextAttachment.alloc().init(), KK2ReproCell.alloc().init(), NSObject.alloc().init()):
    t = type(obj)
    print(t.__name__, slots(t), [b.__name__ for b in t.__mro__][:5])
import objc
print("objc_object", slots(objc.objc_object))
