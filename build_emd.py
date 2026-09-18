# -*- coding: utf-8 -*-
"""시군구 안쪽 읍면동(행정동) 경계를 3단계 화면용으로 만든다.

투영은 새로 계산하지 않고 web/map/*.json 에 이미 들어 있는 proj 계수를
그대로 읽어 쓴다. 시군구 지도와 같은 좌표계라 확대한 화면 위에 그대로
겹쳐진다. build_map.py 를 다시 돌렸다면 이 스크립트도 다시 돌린다.

출력: web/emd/{시도슬러그}.json
      { 시군구: { paths:{동: d}, labels:{동: [x, y, 넓이]} } }

시도별로 한 파일이다. 시군구별로 쪼개면 내려받는 양은 줄지만 파일이
229개가 되어 관리가 번거롭다. 가장 큰 경기도가 300KB 남짓이라
이미 받고 있는 region/gyeonggi.json(483KB)보다 작다.
"""
import json, math, os, sys, collections
import regions as R
from slug import SLUG

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
GEO = os.environ.get("GEO_DIR", os.path.join(ROOT, "geo"))
SUB = os.path.join(GEO, "skorea-submunicipalities-2018-geo.json")
MUNI = os.path.join(GEO, "skorea-municipalities-2018-geo.json")

TOL = 0.0015       # 단순화 허용오차 (시군구 상자 폭 대비)
MIN_AREA = 0.00004  # 이보다 작은 조각은 버린다 (시군구 상자 넓이 대비)


# ------------------------------------------------------------ 시군구 대응표
def parent_table():
    """읍면동 코드 앞 4자리 -> (시도키, 시군구명).

    일반구(수원시 장안구 등)는 읍면동 코드 단계에서 이미 시로 묶인다.
    앞 4자리가 같은 시군구 코드가 여럿이면 collapse_gu 로 합쳐진 이름이
    하나로 모인다.
    """
    out, seen = {}, collections.defaultdict(set)
    for f in json.load(open(MUNI, encoding="utf-8"))["features"]:
        code = f["properties"]["code"]
        name = f["properties"]["name"]
        sido = R.GEO_SIDO[code[:2]]
        sido = R.SGG_MOVE.get((sido, name), sido)
        # 분할·개칭은 resolve() 에서 처리한다. 여기서는 원본 이름을 그대로
        # 둬야 GEO_SPLIT 의 키(옛 이름)와 맞는다.
        seen[code[:4]].add((sido, name))
    for pre, s in seen.items():
        # 일반구(수원시 장안구 등)는 collapse_gu 로 같은 시가 되므로 정상이다.
        final = {(sd, R.collapse_gu(nm)) for sd, nm in s}
        if len(final) > 1:
            print(f"  [경고] 코드 {pre} 가 여러 시군구로 갈립니다: {final}",
                  file=sys.stderr)
        out[pre] = sorted(s)[0]
    return out


def resolve(sido, name, lon, lat):
    """원본 시군구명 -> 현행 시군구명. build_map.py 와 같은 순서로 적용한다.

    2026년 개편으로 갈라진 구는 폴리곤을 자르는 대신 읍면동이 어느 쪽에
    있는지로 판정한다. 경계선이 동을 가르지 않으므로 절단보다 정확하다.
    """
    spec = R.GEO_SPLIT.get((sido, name))
    if spec:
        if spec["kind"] == "parts_by_lon":
            return spec["west"] if lon < spec["boundary"] else spec["east"]
        if spec["kind"] == "cut_by_lat":
            return spec["north"] if lat > spec["boundary"] else spec["south"]
    if (sido, name) in R.GEO_ALSO_MERGE_INTO:
        return R.GEO_ALSO_MERGE_INTO[(sido, name)]
    name = R.collapse_gu(name)
    return R.SGG_RENAME.get((sido, name), name)


# ------------------------------------------------------------ 기하 (외부 의존 없음)
def rings_of(geom):
    """폴리곤의 외곽선 목록. 구멍은 쓰지 않는다."""
    t, c = geom["type"], geom["coordinates"]
    if t == "Polygon":
        return [c[0]]
    if t == "MultiPolygon":
        return [p[0] for p in c]
    return []


def simplify(pts, tol):
    """더글러스-포이커. 반환값은 입력과 같은 순서의 부분열."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    t2 = tol * tol
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        ax, ay = pts[a]; bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        den = dx * dx + dy * dy
        far, fd = -1, -1.0
        for i in range(a + 1, b):
            px, py = pts[i]
            if den == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / den
                t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > fd:
                far, fd = i, d
        if fd > t2:
            keep[far] = True
            stack.append((a, far)); stack.append((far, b))
    return [p for p, k in zip(pts, keep) if k]


def area(pts):
    s = 0.0
    for i in range(len(pts) - 1):
        s += pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
    return abs(s) / 2


def inside(pts, x, y):
    n, ins = len(pts), False
    for i in range(n - 1):
        x1, y1 = pts[i]; x2, y2 = pts[i + 1]
        if (y1 > y) != (y2 > y):
            xx = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xx:
                ins = not ins
    return ins


def label_point(pts):
    """폴리곤 안쪽 한 점. 무게중심이 밖이면 중앙 가로선의 가장 긴
    구간 한가운데를 쓴다. 초승달 모양 동에서 이름이 밖으로 나가지 않게."""
    cx = sum(p[0] for p in pts[:-1]) / (len(pts) - 1)
    cy = sum(p[1] for p in pts[:-1]) / (len(pts) - 1)
    if inside(pts, cx, cy):
        return cx, cy
    ys = sorted(p[1] for p in pts)
    y = ys[len(ys) // 2]
    xs = []
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]; x2, y2 = pts[i + 1]
        if (y1 > y) != (y2 > y):
            xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
    xs.sort()
    best, bw = (cx, cx), -1
    for i in range(0, len(xs) - 1, 2):
        if xs[i + 1] - xs[i] > bw:
            bw, best = xs[i + 1] - xs[i], (xs[i], xs[i + 1])
    return (best[0] + best[1]) / 2, y


# ------------------------------------------------------------ 본체
def build():
    if not os.path.exists(SUB):
        sys.exit(f"원본이 없습니다: {SUB}\n"
                 "southkorea-maps(kostat/2018/json)에서 내려받아 geo/ 에 두세요.")
    parents = parent_table()
    maps = {}
    for fn in os.listdir(os.path.join(WEB, "map")):
        maps[fn[:-5]] = json.load(open(os.path.join(WEB, "map", fn),
                                       encoding="utf-8"))
    rev = {v: k for k, v in SLUG.items()}

    feats = json.load(open(SUB, encoding="utf-8"))["features"]
    bucket = collections.defaultdict(list)     # (시도, 시군구) -> [(동명, rings)]
    for f in feats:
        code = f["properties"]["code"]
        pre = code[:4]
        if pre not in parents:
            continue
        sido, sgg = parents[pre]
        rs = rings_of(f["geometry"])
        if not rs:
            continue
        big = max(rs, key=lambda r: len(r))
        lon = sum(p[0] for p in big) / len(big)
        lat = sum(p[1] for p in big) / len(big)
        sgg = resolve(sido, sgg, lon, lat)
        bucket[(sido, sgg)].append((f["properties"]["name"], rs))

    os.makedirs(os.path.join(WEB, "emd"), exist_ok=True)
    sizes, missing = [], []
    for slug, m in sorted(maps.items()):
        sido = rev[slug]
        p = m["proj"]

        def fn(lon, lat, p=p):
            return ((lon * p["cos"] - p["sx0"]) * p["scale"],
                    p["h"] - (lat - p["minLat"]) * p["scale"])

        out = {}
        for sgg, box in sorted(m.get("boxes", {}).items()):
            rows = bucket.get((sido, sgg))
            if not rows:
                missing.append(f"{sido} {sgg}")
                continue
            bw, bh = box[2], box[3]
            tol = max(bw, bh) * TOL
            minarea = bw * bh * MIN_AREA
            paths, labels = {}, {}
            for name, rs in sorted(rows):
                parts, biggest, ba = [], None, -1.0
                for ring in rs:
                    pts = [fn(lon, lat) for lon, lat in ring]
                    a = area(pts)
                    if a < minarea:
                        continue
                    pts = simplify(pts, tol)
                    if len(pts) < 4:
                        continue
                    parts.append("M" + "L".join(f"{x:.1f},{y:.1f}"
                                                for x, y in pts) + "Z")
                    if a > ba:
                        ba, biggest = a, pts
                if not parts:
                    continue
                paths[name] = "".join(parts)
                lx, ly = label_point(biggest)
                # 셋째 값은 가장 큰 조각의 넓이. 화면에서 이름이 겹칠 때
                # 큰 동부터 자리를 잡게 하는 순서용이다.
                labels[name] = [round(lx, 1), round(ly, 1), round(ba)]
            if paths:
                out[sgg] = {"paths": paths, "labels": labels}
        if not out:
            continue
        dst = os.path.join(WEB, "emd", slug + ".json")
        json.dump(out, open(dst, "w", encoding="utf-8"),
                  ensure_ascii=False, separators=(",", ":"))
        sizes.append((os.path.getsize(dst), slug, len(out)))
        print(f"  {slug}: 시군구 {len(out)}개 · "
              f"{os.path.getsize(dst) // 1024} KB")

    sizes.sort(reverse=True)
    tot = sum(s for s, *_ in sizes)
    print(f"\n읍면동 파일 {len(sizes)}개 · 합계 {tot // 1024} KB · "
          f"가장 큰 {sizes[0][1]} {sizes[0][0] // 1024} KB")
    if missing:
        print(f"\n[경계 없음] {len(missing)}곳: {', '.join(missing[:12])}")


if __name__ == "__main__":
    build()
