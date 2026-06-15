"""Генератор иконки для ярлыка «Торговод · Отгрузки FBO».

Рисует изометрическую посылку в брендовой палитре (тёмный фон + синий)
и сохраняет multi-size .ico для ярлыка на рабочем столе.

Запуск: python tools/make_icon.py
Результат: assets/torgovod.ico (+ assets/torgovod-256.png для предпросмотра)
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ASSETS.mkdir(exist_ok=True)

S = 256  # рендерим крупно, .ico ужмёт до нужных размеров

# Палитра бренда (из base.html)
BG = (13, 17, 23, 255)        # surface-0
BORDER = (33, 38, 45, 255)    # surface-2
TOP = (96, 165, 250, 255)     # #60a5fa — верхняя грань
LEFT = (59, 130, 246, 255)    # #3b82f6 — левая грань
RIGHT = (37, 99, 235, 255)    # #2563eb — правая грань
TAPE = (191, 219, 254, 255)   # #bfdbfe — «скотч»
DOT = (96, 165, 250, 255)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Скруглённая тёмная подложка
d.rounded_rectangle((8, 8, S - 8, S - 8), radius=52, fill=BG, outline=BORDER, width=5)

# Изометрическая коробка
T = (128, 64)    # верхняя вершина
R = (200, 104)   # правая
F = (128, 144)   # передняя (центр)
L = (56, 104)    # левая
Lb = (56, 188)   # левая низ
Fb = (128, 228)  # передняя низ
Rb = (200, 188)  # правая низ

d.polygon([T, R, F, L], fill=TOP)            # крышка
d.polygon([L, F, Fb, Lb], fill=LEFT)         # левая грань
d.polygon([F, R, Rb, Fb], fill=RIGHT)        # правая грань

# «Скотч» — светлая полоса по крышке и вниз по ребру
d.line([T, F], fill=TAPE, width=8)
d.line([F, Fb], fill=TAPE, width=8)
# поперечная полоса по крышке
mid_lr = ((L[0] + R[0]) // 2, (L[1] + R[1]) // 2)
d.line([L, R], fill=TAPE, width=7)

# Акцентная точка (как в фавиконе)
d.ellipse((196, 40, 224, 68), fill=DOT)

# Превью
img.save(ASSETS / "torgovod-256.png")

# .ico с набором размеров
img.save(
    ASSETS / "torgovod.ico",
    format="ICO",
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
print("OK:", ASSETS / "torgovod.ico")
