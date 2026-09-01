"""V4L2 ioctl constants and ctypes structures for raw mmap capture.

These mirror the kernel's <linux/videodev2.h> struct layouts closely
enough for VIDIOC_S_FMT/REQBUFS/QUERYBUF/QBUF/DQBUF/STREAMON/STREAMOFF on
the IPU6 ISYS capture node.
"""

import ctypes

VIDIOC_S_FMT = 0xC0D05605
VIDIOC_REQBUFS = 0xC0145608
VIDIOC_QUERYBUF = 0xC0585609
VIDIOC_QBUF = 0xC058560F
VIDIOC_DQBUF = 0xC0585611
VIDIOC_STREAMON = 0x40045612
VIDIOC_STREAMOFF = 0x40045613
V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_MEMORY_MMAP = 1
V4L2_PIX_FMT_pgAA = 0x41416770


class V4L2PixFormat(ctypes.Structure):
    _fields_ = [
        ('width', ctypes.c_uint32),
        ('height', ctypes.c_uint32),
        ('pixelformat', ctypes.c_uint32),
        ('field', ctypes.c_uint32),
        ('bytesperline', ctypes.c_uint32),
        ('sizeimage', ctypes.c_uint32),
        ('colorspace', ctypes.c_uint32),
        ('priv', ctypes.c_uint32),
        ('flags', ctypes.c_uint32),
        ('ycbcr_enc', ctypes.c_uint32),
        ('quantization', ctypes.c_uint32),
        ('xfer_func', ctypes.c_uint32),
    ]


class V4L2FmtUnion(ctypes.Union):
    _fields_ = [('pix', V4L2PixFormat), ('raw', ctypes.c_uint8 * 200)]


class V4L2Format(ctypes.Structure):
    _fields_ = [('type', ctypes.c_uint32), ('_pad', ctypes.c_uint32), ('fmt', V4L2FmtUnion)]


class V4L2RequestBuffers(ctypes.Structure):
    _fields_ = [
        ('count', ctypes.c_uint32),
        ('type', ctypes.c_uint32),
        ('memory', ctypes.c_uint32),
        ('capabilities', ctypes.c_uint32),
        ('flags', ctypes.c_uint8),
        ('reserved', ctypes.c_uint8 * 3),
    ]


class V4L2Timecode(ctypes.Structure):
    _fields_ = [
        ('type', ctypes.c_uint32),
        ('flags', ctypes.c_uint32),
        ('frames', ctypes.c_uint8),
        ('seconds', ctypes.c_uint8),
        ('minutes', ctypes.c_uint8),
        ('hours', ctypes.c_uint8),
        ('userbits', ctypes.c_uint8 * 4),
    ]


class V4L2BufferM(ctypes.Union):
    _fields_ = [
        ('offset', ctypes.c_uint32),
        ('userptr', ctypes.c_ulong),
        ('planes', ctypes.c_void_p),
        ('fd', ctypes.c_int32),
    ]


class V4L2Buffer(ctypes.Structure):
    _fields_ = [
        ('index', ctypes.c_uint32),
        ('type', ctypes.c_uint32),
        ('bytesused', ctypes.c_uint32),
        ('flags', ctypes.c_uint32),
        ('field', ctypes.c_uint32),
        ('tv_sec', ctypes.c_long),
        ('tv_usec', ctypes.c_long),
        ('timecode', V4L2Timecode),
        ('sequence', ctypes.c_uint32),
        ('memory', ctypes.c_uint32),
        ('m', V4L2BufferM),
        ('length', ctypes.c_uint32),
        ('reserved2', ctypes.c_uint32),
        ('request_fd', ctypes.c_int32),
    ]
