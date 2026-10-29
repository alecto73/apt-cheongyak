# -*- coding: utf-8 -*-
"""바로가기(홈 화면) 아이콘을 만든다.

앱 안에서 쓰는 물방울 핀을 그대로 아이콘으로 삼는다. 배경을 짙은 남색으로
깔고 핀을 종이색으로 비워 내면 홈 화면에서도 또렷하다.

안드로이드의 maskable 아이콘은 가장자리를 깎아낼 수 있으므로, 그림을
가운데 80% 안에 모아 둔다.

  python3 build_icons.py

출력: web/icon.svg, web/icon-{192,512}.png, web/icon-180.png,
      web/favicon.ico, web/manifest.webmanifest
"""
import json, os
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")

BG = (44, 74, 124)        # --blue
PIN = (238, 239, 234)     # --paper
GLYPH = (44, 74, 124)
SS = 8                    # 이만큼 키워 그린 뒤 줄인다 (가장자리를 매끈하게)


def draw_icon(size, pad_ratio=0.17, rounded=True):
    """한 변이 size 인 아이콘 하나."""
    S = size * SS
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    if rounded:
        d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=BG)
    else:
        d.rectangle([0, 0, S - 1, S - 1], fill=BG)

    # 핀: 머리 원 + 꼬리 삼각형을 같은 색으로 겹쳐 물방울을 만든다
    inner = S * (1 - pad_ratio * 2)       # 그림이 들어갈 네모
    ph = inner * 0.92                      # 핀 전체 높이
    r = ph * 0.345                         # 머리 반지름
    cx = S / 2
    cy = S / 2 - ph / 2 + r + S * 0.012
    tip = cy + ph - r

    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=PIN)
    k = 0.78
    d.polygon([(cx - r * k, cy + r * 0.62), (cx + r * k, cy + r * 0.62),
               (cx, tip)], fill=PIN)

    # 머리 안의 아파트
    g = r * 0.62                           # 글리프 반폭
    bx0, bx1 = cx - g, cx + g
    roof = cy - g * 0.95
    body = cy - g * 0.18
    base = cy + g * 0.98
    d.polygon([(bx0, body), (cx, roof), (bx1, body)], fill=GLYPH)
    d.rectangle([bx0, body, bx1, base], fill=GLYPH)

    # 창문 두 줄
    w = g * 0.30
    gap = g * 0.26
    for row in range(2):
        yy = body + g * 0.30 + row * (w + gap * 0.9)
        for col in (-1, 1):
            xx = cx + col * (gap * 0.72) - w / 2
            d.rectangle([xx, yy, xx + w, yy + w], fill=PIN)

    return im.resize((size, size), Image.LANCZOS)


SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="113" fill="#2c4a7c"/>
  <path d="M256 92c-57 0-103 46-103 103 0 79 103 225 103 225s103-146 103-225c0-57-46-103-103-103z"
        fill="#eeefea"/>
  <path d="M256 139l-47 44v104h94V183z" fill="#2c4a7c"/>
  <rect x="226" y="205" width="22" height="22" fill="#eeefea"/>
  <rect x="264" y="205" width="22" height="22" fill="#eeefea"/>
  <rect x="226" y="243" width="22" height="22" fill="#eeefea"/>
  <rect x="264" y="243" width="22" height="22" fill="#eeefea"/>
</svg>
'''

MANIFEST = {
    "name": "청약·미분양·대장아파트 지도",
    "short_name": "청약지도",
    "description": "최근 3년 청약 결과와 준공후 미분양, 지역별 대장아파트를 "
                   "지도 한 화면에서 봅니다.",
    "start_url": "./",
    "scope": "./",
    "display": "standalone",
    "orientation": "any",
    "background_color": "#eeefea",
    "theme_color": "#eeefea",
    "lang": "ko",
    "icons": [
        {"src": "icon-192.png", "sizes": "192x192", "type": "image/png",
         "purpose": "any"},
        {"src": "icon-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "any"},
        {"src": "icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "maskable"},
        {"src": "icon.svg", "sizes": "any", "type": "image/svg+xml"},
    ],
}


def main():
    os.makedirs(WEB, exist_ok=True)

    for size in (512, 192, 180):
        draw_icon(size).save(f"{WEB}/icon-{size}.png")

    # maskable: 가장자리가 깎여도 괜찮게 여백을 더 주고 모서리를 채운다
    draw_icon(512, pad_ratio=0.26, rounded=False).save(
        f"{WEB}/icon-maskable-512.png")

    # 파비콘: 작은 크기를 함께 담는다
    ico = draw_icon(64)
    ico.save(f"{WEB}/favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])

    open(f"{WEB}/icon.svg", "w", encoding="utf-8").write(SVG)
    json.dump(MANIFEST, open(f"{WEB}/manifest.webmanifest", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    for f in ("icon.svg", "icon-180.png", "icon-192.png", "icon-512.png",
              "icon-maskable-512.png", "favicon.ico", "manifest.webmanifest"):
        print(f"  web/{f}  {os.path.getsize(f'{WEB}/{f}'):,} 바이트")


if __name__ == "__main__":
    main()
