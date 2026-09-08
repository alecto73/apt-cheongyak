# -*- coding: utf-8 -*-
"""2018년 기준 시군구 경계를 현행 행정구역으로 재구성하고
모바일에서 쓸 수 있는 크기의 SVG 경로로 변환한다.

출력: web/map.json
  { "sido": {코드: {name, viewBox, paths:{sgg: d}}}, "national": {...} }
"""
import json, math, sys, collections
from shapely.geometry import shape, box, mapping
from shapely.ops import unary_union
import regions as R
from slug import SLUG

MUNI = "/home/claude/geo/muni.json"


def load_units():
    """(시도키, 시군구명) -> shapely geometry"""
    feats = json.load(open(MUNI))["features"]
    bucket = collections.defaultdict(list)
    for f in feats:
        code = f["properties"]["code"]
        name = f["properties"]["name"]
        sido = R.GEO_SIDO[code[:2]]
        sido = R.SGG_MOVE.get((sido, name), sido)      # 군위군 -> 대구
        geom = shape(f["geometry"]).buffer(0)

        split = R.GEO_SPLIT.get((sido, name))
        if split:
            for sub_name, sub_geom in apply_split(geom, split):
                bucket[(sido, sub_name)].append(sub_geom)
            continue

        merge_to = R.GEO_ALSO_MERGE_INTO.get((sido, name))
        if merge_to:
            bucket[(sido, merge_to)].append(geom)
            continue

        name = R.collapse_gu(name)
        name = R.SGG_RENAME.get((sido, name), name)
        bucket[(sido, name)].append(geom)

    return {k: unary_union(v).buffer(0) for k, v in bucket.items()}


def apply_split(geom, spec):
    if spec["kind"] == "parts_by_lon":
        west, east = [], []
        parts = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
        for p in parts:
            (west if p.centroid.x < spec["boundary"] else east).append(p)
        return [(spec["west"], unary_union(west)), (spec["east"], unary_union(east))]
    if spec["kind"] == "cut_by_lat":
        minx, miny, maxx, maxy = geom.bounds
        b = spec["boundary"]
        north = geom.intersection(box(minx - 1, b, maxx + 1, maxy + 1))
        south = geom.intersection(box(minx - 1, miny - 1, maxx + 1, b))
        return [(spec["north"], north), (spec["south"], south)]
    raise ValueError(spec["kind"])


# ------------------------------------------------------------ 투영 / 단순화
def project(lon, lat, lat0):
    """정적 지도용 등거리원통도법. 중위도 보정만 한다."""
    return lon * math.cos(math.radians(lat0)), lat


def rings(geom, min_area):
    """면적이 min_area 이상인 폴리곤의 외곽선만 추린다 (섬 노이즈 제거)."""
    out = []
    parts = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    for p in parts:
        if p.is_empty or p.area < min_area:
            continue
        out.append(list(p.exterior.coords))
    return out


def transform(bbox, size, lat0):
    """경계상자를 화면 좌표로 옮기는 함수와 뷰박스 크기를 돌려준다.

    정사각형에 맞추면 남북으로 긴 지역에서 좌우 여백이 크게 남는다.
    실제 비율대로 뷰박스를 잡아 지도가 화면을 채우게 한다.
    """
    minx, miny, maxx, maxy = bbox
    sx0, _ = project(minx, miny, lat0)
    sx1, _ = project(maxx, miny, lat0)
    w, h = sx1 - sx0, maxy - miny
    scale = size / max(w, h)
    W, H = w * scale, h * scale

    def fn(lon, lat):
        x, _ = project(lon, lat, lat0)
        return (x - sx0) * scale, H - (lat - miny) * scale
    return fn, W, H


def to_path(ring_list, fn):
    parts = []
    for ring in ring_list:
        pts = []
        for lon, lat in ring:
            px, py = fn(lon, lat)
            pts.append(f"{px:.1f},{py:.1f}")
        if len(pts) > 2:
            parts.append("M" + "L".join(pts) + "Z")
    return "".join(parts)


def build(size=1000):
    units = load_units()

    # KOSIS 기준 목록과 대조
    kosis = collections.defaultdict(set)
    for r in json.load(open("/home/claude/raw/unsold_total.json")):
        if r["C2_NM"] not in ("계", "합계"):
            kosis[r["C1_NM"]].add(r["C2_NM"])
    ours = collections.defaultdict(set)
    for sido, sgg in units:
        ours[sido].add(sgg)
    problems = []
    for sido in sorted(set(kosis) | set(ours)):
        miss = kosis[sido] - ours[sido]
        extra = ours[sido] - kosis[sido]
        if miss or extra:
            problems.append((sido, sorted(miss), sorted(extra)))
    if problems:
        print("[경계-통계 불일치]", file=sys.stderr)
        for p in problems:
            print("  ", p, file=sys.stderr)
    else:
        print(f"[검증] 시도 {len(ours)}개 / 시군구 {sum(len(v) for v in ours.values())}개 완전 일치")

    out = {"national": None, "sido": {}}

    # 시도 단위 지도
    sido_geom = {}
    for sido in R.SIDO_ORDER:
        gs = [g for (s, _), g in units.items() if s == sido]
        sido_geom[sido] = unary_union(gs).buffer(0)

    nat_bbox = R.NATIONAL_BBOX
    nat_view = box(*nat_bbox)
    lat0 = (nat_bbox[1] + nat_bbox[3]) / 2
    fn, W, H = transform(nat_bbox, size, lat0)
    nat_paths, nat_labels = {}, {}
    for sido, g in sido_geom.items():
        clipped = g.intersection(nat_view)
        simp = clipped.simplify(0.006, preserve_topology=True)
        nat_paths[sido] = to_path(rings(simp, 0.004), fn)
        nat_labels[sido] = label_point(clipped, fn)
    out["national"] = {"viewBox": f"0 0 {W:.0f} {H:.0f}",
                       "paths": nat_paths, "labels": nat_labels}

    # 시군구 단위 지도
    for sido in R.SIDO_ORDER:
        subs = {sgg: g for (s, sgg), g in units.items() if s == sido}
        bbox = core_bbox(list(subs.values()))
        l0 = (bbox[1] + bbox[3]) / 2
        span = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
        tol = span * 0.0015
        minarea = (span ** 2) * 0.00025
        view = box(*bbox)
        fn, W, H = transform(bbox, size, l0)
        paths, labels, offmap = {}, {}, []
        for sgg, g in subs.items():
            visible = g.intersection(view)
            if visible.is_empty or visible.area < g.area * 0.02:
                offmap.append(sgg)          # 지도 밖: 목록으로 선택
                continue
            simp = visible.simplify(tol, preserve_topology=True)
            rl = rings(simp, minarea) or rings(simp, 0)
            paths[sgg] = to_path(rl, fn)
            labels[sgg] = label_point(visible, fn)
        out["sido"][sido] = {"name": R.SIDO_FULL[sido],
                             "viewBox": f"0 0 {W:.0f} {H:.0f}",
                             "paths": paths, "labels": labels,
                             "offmap": sorted(offmap)}
        if offmap:
            print(f"  [지도밖] {sido}: {', '.join(sorted(offmap))}")
    return out


def core_bbox(geoms, near=0.10):
    """본토 군집만으로 경계상자를 잡는다.

    옹진군 백령도나 신안군 가거도처럼 멀리 떨어진 섬을 그대로 두면
    시도 경계상자가 바다까지 커져 본토가 점처럼 작아진다.
    가장 큰 조각에서 출발해, 현재 상자 크기의 near 배 안에 있는
    조각만 반복해서 흡수한다. 멀리 있는 섬은 군집에 들어오지 못한다.
    """
    parts = []
    for g in geoms:
        parts += list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
    parts.sort(key=lambda p: -p.area)
    cluster = [parts[0]]
    rest = parts[1:]
    while True:
        cur = unary_union(cluster)
        b = cur.bounds
        span = max(b[2] - b[0], b[3] - b[1])
        limit = span * near
        added = [p for p in rest if cur.distance(p) <= limit]
        if not added:
            return b
        cluster += added
        rest = [p for p in rest if p not in added]


def label_point(geom, fn):
    """라벨을 얹을 좌표. 가장 큰 조각의 대표점을 쓴다."""
    parts = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    big = max(parts, key=lambda p: p.area)
    p = big.representative_point()
    x, y = fn(p.x, p.y)
    return [round(x, 1), round(y, 1)]


if __name__ == "__main__":
    data = build()
    import os
    # 첫 화면에는 전국 지도만 필요하다. 시군구 경계는 시도를 고른 뒤 받는다.
    os.makedirs("web/map", exist_ok=True)
    json.dump(data["national"], open("web/map-national.json", "w"),
              ensure_ascii=False, separators=(",", ":"))
    for sido, m in data["sido"].items():
        json.dump(m, open(f"web/map/{SLUG[sido]}.json", "w"),
                  ensure_ascii=False, separators=(",", ":"))
    nat = os.path.getsize("web/map-national.json") // 1024
    big = max(os.path.getsize(f"web/map/{f}") for f in os.listdir("web/map")) // 1024
    print(f"web/map-national.json {nat} KB / 시도 경계 최대 {big} KB")
