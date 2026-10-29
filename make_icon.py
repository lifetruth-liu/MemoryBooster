# -*- coding: utf-8 -*-
"""生成应用图标：圆角渐变底 + 内存芯片 + 闪电

输出：
  assets/icon.ico   多尺寸（16/20/24/32/40/48/64/128/256），供 exe 与窗口图标
  assets/icon.png   256x256，供文档/预览
运行：py -3.10 make_icon.py
"""
import os
import struct
from PIL import Image, ImageDraw, ImageFilter

S = 1024          # 超采样画布
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

# 品牌色（与界面强调色一致）
C_TOP = (58, 150, 240)     # 浅蓝
C_BOT = (9, 78, 178)       # 深蓝
C_BOLT = (255, 255, 255)


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def vertical_gradient(size, top, bot):
    img = Image.new("RGB", (1, size))
    d = ImageDraw.Draw(img)
    for y in range(size):
        t = y / max(1, size - 1)
        # 轻微缓出，让上半部分更亮一点
        t = t ** 0.92
        d.point((0, y), fill=(
            int(top[0] + (bot[0] - top[0]) * t),
            int(top[1] + (bot[1] - top[1]) * t),
            int(top[2] + (bot[2] - top[2]) * t),
        ))
    return img.resize((size, size), Image.BILINEAR)


def _bmp_frame(img):
    """把一张 RGBA 图编码成 ICO 内的 BMP 帧（BITMAPINFOHEADER + BGRA 倒序 + AND 掩码）。

    为什么不用 PIL 的 ICO 保存：Pillow>=9.4 默认 bitmap_format="png"，会把 16~128
    这些小尺寸也压成 PNG。而 Windows Shell 只在 256x256 这一档支持 PNG 帧，
    小尺寸用 PNG 会导致资源管理器/任务栏显示空白或通用图标。
    """
    s = img.size[0]
    px = img.convert("RGBA").load()
    rows = []
    for y in range(s - 1, -1, -1):          # BMP 是自下而上
        row = bytearray()
        for x in range(s):
            r, g, b, a = px[x, y]
            row += bytes((b, g, r, a))      # BGRA
        rows.append(bytes(row))
    xor = b"".join(rows)
    stride = ((s + 31) // 32) * 4           # AND 掩码每行 1bpp，按 4 字节对齐
    and_mask = bytes(stride * s)            # 全 0：透明交给 alpha 通道
    bih = struct.pack("<IiiHHIIiiII", 40, s, s * 2, 1, 32, 0, len(xor), 0, 0, 0, 0)
    return bih + xor + and_mask


def _write_ico(path, imgs):
    """手写 ICO：<=128 用 BMP 帧，256 用 PNG 帧（兼顾外壳兼容与体积）。"""
    import io

    entries, blobs = b"", b""
    offset = 6 + 16 * len(imgs)
    for img in imgs:
        s = img.size[0]
        if s >= 256:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            data = buf.getvalue()
        else:
            data = _bmp_frame(img)
        dim = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    with open(path, "wb") as f:
        f.write(struct.pack("<HHH", 0, 1, len(imgs)) + entries + blobs)


def build():
    os.makedirs(OUT_DIR, exist_ok=True)
    size = S

    # 1) 渐变底
    bg = vertical_gradient(size, C_TOP, C_BOT).convert("RGBA")

    # 2) 顶部高光（柔和的斜向亮斑，增加质感）
    gloss = Image.new("L", (size, size), 0)
    gd = ImageDraw.Draw(gloss)
    gd.ellipse([-size * 0.35, -size * 0.55, size * 0.85, size * 0.45], fill=90)
    gloss = gloss.filter(ImageFilter.GaussianBlur(size * 0.09))
    white = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    bg = Image.composite(white, bg, gloss.point(lambda v: int(v * 0.55)))

    # 3) 内存芯片轮廓（浅白色描边）
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    lw = int(size * 0.035)
    chip = [size * 0.20, size * 0.20, size * 0.80, size * 0.80]
    d.rounded_rectangle(chip, radius=size * 0.13,
                        outline=(255, 255, 255, 110), width=lw)
    # 芯片引脚：每边 3 根
    pin_len = size * 0.075
    pin_w = size * 0.030
    for i in (0.34, 0.50, 0.66):
        # 上
        d.rounded_rectangle([size * i - pin_w / 2, size * 0.20 - pin_len,
                             size * i + pin_w / 2, size * 0.20 + size * 0.01],
                            radius=pin_w / 2, fill=(255, 255, 255, 110))
        # 下
        d.rounded_rectangle([size * i - pin_w / 2, size * 0.79,
                             size * i + pin_w / 2, size * 0.79 + pin_len],
                            radius=pin_w / 2, fill=(255, 255, 255, 110))
        # 左
        d.rounded_rectangle([size * 0.20 - pin_len, size * i - pin_w / 2,
                             size * 0.20 + size * 0.01, size * i + pin_w / 2],
                            radius=pin_w / 2, fill=(255, 255, 255, 110))
        # 右
        d.rounded_rectangle([size * 0.79, size * i - pin_w / 2,
                             size * 0.79 + pin_len, size * i + pin_w / 2],
                            radius=pin_w / 2, fill=(255, 255, 255, 110))
    bg = Image.alpha_composite(bg, layer)

    # 4) 闪电（白色，带轻微投影）
    d = ImageDraw.Draw(bg)
    raw = [
        (0.560, 0.250),
        (0.365, 0.538),
        (0.487, 0.538),
        (0.450, 0.748),
        (0.645, 0.452),
        (0.517, 0.452),
        (0.592, 0.250),
    ]
    k = 0.97   # 整体缩放，避免下尖顶到芯片边框
    bolt = [(((x - 0.5) * k + 0.5) * size, ((y - 0.5) * k + 0.5) * size - size * 0.012)
            for x, y in raw]
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    off = size * 0.012
    sd.polygon([(x + off, y + off) for x, y in bolt], fill=(0, 30, 70, 90))
    shadow = shadow.filter(ImageFilter.GaussianBlur(size * 0.012))
    bg = Image.alpha_composite(bg, shadow)

    d = ImageDraw.Draw(bg)
    d.polygon(bolt, fill=C_BOLT)

    # 5) 圆角裁切
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(bg, (0, 0), rounded_mask(size, int(size * 0.22)))

    # 6) 导出
    png_path = os.path.join(OUT_DIR, "icon.png")
    out.resize((256, 256), Image.LANCZOS).save(png_path)

    ico_path = os.path.join(OUT_DIR, "icon.ico")
    sizes = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40),
             (48, 48), (64, 64), (128, 128), (256, 256)]
    imgs = [out.resize(s, Image.LANCZOS) for s in sizes]
    _write_ico(ico_path, imgs)
    print("written:", png_path)
    print("written:", ico_path, os.path.getsize(ico_path), "bytes")


if __name__ == "__main__":
    build()
