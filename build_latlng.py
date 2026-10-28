# -*- coding: utf-8 -*-
"""SVG 경로로 저장된 경계를 위경도 폴리곤으로 되돌린다.

지도를 카카오맵으로 바꾸면서 필요해졌다. 기존 경계 파일
(`web/map-national.json`, `web/map/*.json`, `web/emd/*.json`)은 화면
좌표로 투영된 SVG 경로라 지도 위에 그대로 올릴 수 없다.

다시 내려받아 만들지 않고 투영을 거꾸로 푼다. `build_map.py` 의
`transform()` 이 선형 변환이라 역산이 정확하고, 그동안 쌓아 둔
2026년 행정구역 개편(인천 분구, 전남광주 통합) 처리도 그대로 남는다.

  lon = (x / scale + sx0) / cos
  lat = minLat + (h - y) / scale

전국 지도에는 `proj` 가 저장돼 있지 않다. `build_map.py` 가 쓴 것과
같은 상수(NATIONAL_BBOX, size=1000)로 다시 계산한다. 결과가 파일의
viewBox 와 맞는지 먼저 확인하고 진행한다.

출력:
  web/geo/sido.json          {시도: {rings, label}}
  web/geo/sgg/{슬러그}.json   {시군구: {rings, label, box}}
  web/geo/emd/{슬러그}.json   {시군구: {읍면동: {rings, label}}}

좌표는 [위도, 경도] 순서다. 카카오맵 `LatLng` 와 같은 순서여서
프론트에서 그대로 넘길 수 있다.
"""
import json, math, os, sys
import regions as R
from slug import SLUG

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
OUT = os.path.join(WEB, "geo")
ND = 5          # 소수점 다섯 자리 ≈ 1.1m. 경계선에는 이걸로 충분하다.


def national_proj(size=1000):
    """build_map.py 의 transform() 을 전국 뷰포트에 대해 재현한다."""
    minx, miny, maxx, maxy = R.NATIONAL_BBOX
    lat0 = (miny + maxy) / 2
    cos = math.cos(math.radians(lat0))
    sx0 = minx * cos
    w, h = maxx * cos - sx0, maxy - miny
    scale = size / max(w, h)
    return {"cos": round(cos, 8), "sx0": round(sx0, 6),
            "minLat": round(miny, 6), "scale": round(scale, 6),
            "h": round(h * scale, 3)}, w * scale, h * scale


def inverter(p):
    cos, sx0, minLat, scale, h = (p["cos"], p["sx0"], p["minLat"],
                                  p["scale"], p["h"])

    def back(x, y):
        return (round(minLat + (h - y) / scale, ND),
                round((x / scale + sx0) / cos, ND))
    return back


def rings_of(d, back):
    """'M x,y L x,y ... Z' 를 [[위도, 경도], ...] 목록으로."""
    out = []
    for part in d.split("M"):
        part = part.strip().rstrip("Z").strip()
        if not part:
            continue
        pts = []
        for pair in part.split("L"):
            pair = pair.strip()
            if not pair:
                continue
            x, y = pair.split(",")
            pts.append(list(back(float(x), float(y))))
        if len(pts) >= 4:
            out.append(pts)
    return out


def bbox_of(rings):
    """[남서위, 남서경, 북동위, 북동경]. 지도를 그 지역으로 맞출 때 쓴다."""
    lats = [p[0] for r in rings for p in r]
    lngs = [p[1] for r in rings for p in r]
    return [round(min(lats), ND), round(min(lngs), ND),
            round(max(lats), ND), round(max(lngs), ND)]


def convert(paths, labels, back, with_box=False):
    out = {}
    for name, d in paths.items():
        rings = rings_of(d, back)
        if not rings:
            continue
        rec = {"rings": rings}
        lab = (labels or {}).get(name)
        if lab:
            rec["label"] = list(back(lab[0], lab[1]))
        if with_box:
            rec["box"] = bbox_of(rings)
        out[name] = rec
    return out


def main():
    os.makedirs(os.path.join(OUT, "sgg"), exist_ok=True)
    os.makedirs(os.path.join(OUT, "emd"), exist_ok=True)

    # ── 시도 ──────────────────────────────────────────────
    nat = json.load(open(f"{WEB}/map-national.json", encoding="utf-8"))
    p, W, H = national_proj()
    want = f"0 0 {W:.0f} {H:.0f}"
    if nat["viewBox"] != want:
        sys.exit(f"전국 투영이 맞지 않습니다: 계산 {want} / 파일 "
                 f"{nat['viewBox']}\nbuild_map.py 의 NATIONAL_BBOX 나 "
                 "size 가 바뀌었는지 확인하세요.")
    sido = convert(nat["paths"], nat.get("labels"), inverter(p), with_box=True)
    json.dump(sido, open(f"{OUT}/sido.json", "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    print(f"  시도 {len(sido)}개 · {os.path.getsize(f'{OUT}/sido.json') // 1024} KB")

    # ── 시군구 · 읍면동 ───────────────────────────────────
    tot_sgg = tot_emd = 0
    for sido_name in R.SIDO_ORDER:
        s = SLUG[sido_name]
        m = json.load(open(f"{WEB}/map/{s}.json", encoding="utf-8"))
        back = inverter(m["proj"])
        sgg = convert(m["paths"], m.get("labels"), back, with_box=True)
        json.dump(sgg, open(f"{OUT}/sgg/{s}.json", "w", encoding="utf-8"),
                  ensure_ascii=False, separators=(",", ":"))
        tot_sgg += os.path.getsize(f"{OUT}/sgg/{s}.json")

        # 읍면동은 같은 시도의 투영을 쓴다 (build_emd.py 가 그렇게 만든다)
        ep = f"{WEB}/emd/{s}.json"
        if os.path.exists(ep):
            src = json.load(open(ep, encoding="utf-8"))
            emd = {g: convert(v["paths"], v.get("labels"), back)
                   for g, v in src.items()}
            emd = {g: v for g, v in emd.items() if v}
            json.dump(emd, open(f"{OUT}/emd/{s}.json", "w", encoding="utf-8"),
                      ensure_ascii=False, separators=(",", ":"))
            tot_emd += os.path.getsize(f"{OUT}/emd/{s}.json")
        print(f"  {s}: 시군구 {len(sgg)}개")

    print(f"\n시군구 합계 {tot_sgg // 1024} KB · 읍면동 합계 {tot_emd // 1024} KB")


if __name__ == "__main__":
    main()
