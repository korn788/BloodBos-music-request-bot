# make_icons.py — render src/assets/icon.svg into the PNG / ICO / ICNS files the
# installers need. Run from anywhere:  python packaging/make_icons.py
import io
import os
import sys

from PyQt6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PyQt6.QtGui import QGuiApplication, QImage, QPainter
from PyQt6.QtSvg import QSvgRenderer

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "..", "src", "assets")


def render(svg, size):
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    svg.render(p)
    p.end()
    return img


def png_bytes(img):
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication(sys.argv)  # noqa: F841
    svg = QSvgRenderer(os.path.join(ASSETS, "icon.svg"))
    if not svg.isValid():
        raise SystemExit("icon.svg didn't load")
    sizes = [16, 24, 32, 48, 64, 128, 256, 512, 1024]
    pngs = {s: png_bytes(render(svg, s)) for s in sizes}
    with open(os.path.join(ASSETS, "icon.png"), "wb") as f:
        f.write(pngs[256])
    with open(os.path.join(ASSETS, "icon-512.png"), "wb") as f:
        f.write(pngs[512])

    from PIL import Image
    big = Image.open(io.BytesIO(pngs[1024]))
    big.save(os.path.join(ASSETS, "icon.ico"),
             sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    big.save(os.path.join(ASSETS, "icon.icns"))
    print("icons written to", os.path.abspath(ASSETS))


if __name__ == "__main__":
    main()
