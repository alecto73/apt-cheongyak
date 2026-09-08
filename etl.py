# -*- coding: utf-8 -*-
"""청약홈 APT 분양정보와 KOSIS 미분양 통계를 시군구 단위로 합쳐
프론트엔드가 바로 읽는 web/data.json 을 만든다.

  python etl.py            # 전체 수집 후 집계
  python etl.py --offline  # 이미 받아둔 raw/ 파일로 집계만

환경변수 DATA_GO_KR_KEY, KOSIS_KEY 필요 (.env 도 읽는다).
호출량은 전량 수집 기준 20회 미만이다. 청약홈 개발계정 일 40,000건에
비하면 무시할 수준이므로 증분 수집 없이 매일 전체를 새로 받는다.
"""
import json, os, sys, time, math, urllib.parse, urllib.request, collections
from datetime import date, timedelta

import regions as R
from slug import SLUG

ROOT = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(ROOT, "raw")
WEB = os.path.join(ROOT, "web")

ODCLOUD = "https://api.odcloud.kr/api"
KOSIS = "https://kosis.kr/openapi/Param/statisticsParameterData.do"

# KOSIS 미분양주택현황보고 (국토교통부, orgId=116)
UNSOLD_TABLES = {
    "total": {"tblId": "DT_MLTM_2082", "itmId": "13103871087T1",
              "obj": "13102871087A", "label": "시·군·구별 미분양현황"},
    # 준공후 통계표에는 부문·규모 축이 더 있어 '계'를 지정해야 총계가 나온다.
    "after": {"tblId": "DT_MLTM_5328", "itmId": "13103871088T1",
              "obj": "13102871088A", "label": "공사완료후 미분양현황",
              "extra": {"objL3": "13102871088C.0001",
                        "objL4": "13102871088D.0001"}},
}

# 조회 대상 기간. 3년치면 공고 1,000건 남짓으로 여전히 한 번에 받을 수 있다.
YEARS_BACK = 3


# --------------------------------------------------------------- 공통 유틸
def load_env():
    p = os.path.join(ROOT, ".env")
    if os.path.exists(p):
        for line in open(p):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)


def get_json(url, tries=5):
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=180) as r:
                return json.load(r)
        except Exception as e:      # 초당 호출 제한, 일시적 게이트웨이 오류
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"요청 실패: {url[:90]}... ({last})")


# --------------------------------------------------------------- 수집
def fetch_applyhome(key):
    """분양정보 / 주택형별 상세 / 경쟁률 전량. perPage 상한이 커서
    공고별 반복 호출(N+1) 없이 통째로 페이징하면 된다."""
    specs = {
        "notice": ("ApplyhomeInfoDetailSvc", "getAPTLttotPblancDetail"),
        "model": ("ApplyhomeInfoDetailSvc", "getAPTLttotPblancMdl"),
        "cmpet": ("ApplyhomeInfoCmpetRtSvc", "getAPTLttotPblancCmpet"),
    }
    out = {}
    for name, (svc, op) in specs.items():
        rows, page = [], 1
        while True:
            q = urllib.parse.urlencode({"serviceKey": key, "page": page,
                                        "perPage": 2000})
            d = get_json(f"{ODCLOUD}/{svc}/v1/{op}?{q}")
            if "data" not in d:
                raise RuntimeError(f"{op}: {d}")
            rows += d["data"]
            if len(rows) >= d["totalCount"] or not d["data"]:
                break
            page += 1
            time.sleep(0.4)
        json.dump(rows, open(f"{RAW}/{name}.json", "w"), ensure_ascii=False)
        out[name] = rows
        print(f"  청약홈 {name}: {len(rows):,}건")
    return out


def fetch_unsold(key):
    """미분양은 월 1회 갱신이므로 최신 1개 시점만 받는다."""
    out = {}
    for kind, spec in UNSOLD_TABLES.items():
        meta_url = ("https://kosis.kr/openapi/statisticsData.do?"
                    + urllib.parse.urlencode(
                        {"method": "getMeta", "apiKey": key, "orgId": "116",
                         "tblId": spec["tblId"], "type": "ITM",
                         "format": "json", "jsonVD": "Y"}))
        meta = get_json(meta_url)
        sidos = [r["ITM_ID"] for r in meta
                 if r.get("OBJ_NM") == "구분" and r["ITM_NM"] != "전국"]
        rows = []
        for sid in sidos:
            p = {"method": "getList", "apiKey": key, "format": "json",
                 "jsonVD": "Y", "orgId": "116", "tblId": spec["tblId"],
                 "itmId": spec["itmId"], "objL1": sid, "objL2": "ALL",
                 "prdSe": "M", "newEstPrdCnt": "1"}
            p.update(spec.get("extra") or {})
            d = get_json(KOSIS + "?" + urllib.parse.urlencode(p))
            if isinstance(d, list):
                rows += d
            time.sleep(0.25)
        json.dump(rows, open(f"{RAW}/unsold_{kind}.json", "w"),
                  ensure_ascii=False)
        out[kind] = rows
        print(f"  미분양 {spec['label']}: {len(rows):,}건")
    return out


# --------------------------------------------------------------- 주소 정규화
class Locator:
    """공급위치 주소 문자열을 (시도, 시군구)로 바꾼다.

    청약홈 API의 지역 필드는 시도까지만 있어서 시군구는 주소를 파싱해야
    한다. 주소 표기가 자유 서술형이라 규칙을 겹겹이 쌓아 처리한다.
    """

    def __init__(self, canon):
        self.canon = canon                      # {시도: set(시군구)}
        self.stems = {}                         # 접미사 뗀 이름 -> 정식명
        for sido, names in canon.items():
            self.stems[sido] = {n.rstrip("시군구"): n for n in names}
        self.unresolved = []

    @staticmethod
    def _clean(addr):
        a = (addr or "").replace("\u00a0", " ").strip()
        # "김포 풍무역세권 B4블록 (경기도 김포시 사우동 458)" 처럼
        # 괄호 안에 진짜 주소가 들어있는 경우가 많다. 괄호를 우선 본다.
        cands = []
        depth, buf = 0, ""
        for ch in a:
            if ch == "(":
                depth += 1
                if depth == 1:
                    buf = ""
                    continue
            if ch == ")":
                depth -= 1
                if depth == 0 and buf.strip():
                    cands.append(buf.strip())
                continue
            if depth >= 1:
                buf += ch
        cands.append(a)
        return [c.replace("특례시", "시") for c in cands]

    def locate(self, addr, area_hint=None, extra_text=""):
        for text in self._clean(addr):
            hit = self._try(text, area_hint)
            if hit:
                return hit
        # 주소에 시군구가 없는 사례: 단지명이나 사업지구명으로 재시도
        hit = self._try(extra_text, area_hint, loose=True)
        if hit:
            return hit
        self.unresolved.append((addr, area_hint))
        return None, None

    def _try(self, text, area_hint, loose=False):
        toks = text.split()
        sido = None
        if toks:
            sido = R.SIDO_ALIAS.get(toks[0]) or R.SIDO_ALIAS_AMBIGUOUS.get(toks[0])
        if not sido and area_hint:
            sido = R.SIDO_ALIAS.get(area_hint) or R.SIDO_ALIAS_AMBIGUOUS.get(area_hint)
        if not sido:
            return None
        names = self.canon[sido]

        if sido == "세종":
            return sido, next(iter(names))

        # 1) 정식 시군구명이 토큰으로 그대로 등장
        for t in toks:
            if t in names:
                return sido, t
        # 2) 인천 개편으로 사라진 구 (중구/동구/서구) -> 동 이름으로 판정
        if sido == "인천":
            got = self._incheon(text, toks)
            if got:
                return sido, got
        # 3) 폐지·개칭된 이름
        for t in toks:
            renamed = R.SGG_RENAME.get((sido, t))
            if renamed in names:
                return sido, renamed
            collapsed = R.SGG_RENAME.get((sido, R.collapse_gu(t)), R.collapse_gu(t))
            if collapsed in names:
                return sido, collapsed
        # 4) 접미사 없는 표기 ("원주무실", "천안아산")
        stems = self.stems[sido]
        for t in toks if not loose else [text]:
            for stem, full in stems.items():
                if len(stem) >= 2 and stem in t:
                    return sido, full
        return None

    def _incheon(self, text, toks):
        for old, (a, b, a_set) in R.INCHEON_OLD_SPLIT.items():
            if old not in toks:
                continue
            for t in toks:
                if t in a_set:
                    return a
                if t in R.INCHEON_SEOHAE_DONG and old == "서구":
                    return b
            # 동 이름이 없으면 사업지구명으로 판정
            if old == "중구":
                if any(k in text for k in ("영종", "하늘도시", "운서", "운남")):
                    return "영종구"
                return "제물포구"
            if old == "서구":
                if "검단" in text:
                    return "검단구"
                if any(k in text for k in ("청라", "검암", "루원", "가정")):
                    return "서해구"
                return None
        if "동구" in toks:
            return R.INCHEON_DONGGU_TO
        return None


# --------------------------------------------------------------- 집계
RANK_ORDER = [(1, "해당지역"), (1, "기타경기"), (1, "기타지역"),
              (2, "해당지역"), (2, "기타경기"), (2, "기타지역")]


def num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def pyeong_price(amount_manwon, supply_ar):
    """만원 단위 공급금액과 공급면적(㎡)으로 평당가(만원)를 낸다."""
    if not amount_manwon or not supply_ar:
        return None
    return round(amount_manwon / (supply_ar / 3.3058))


def build_types(models, cmpets):
    """주택형별 공급세대수·분양가·청약결과."""
    by_cm = collections.defaultdict(list)
    for c in cmpets:
        by_cm[(c["HOUSE_TY"], c.get("MODEL_NO"))].append(c)

    types = []
    for m in sorted(models, key=lambda x: x.get("MODEL_NO") or ""):
        key = (m["HOUSE_TY"], m.get("MODEL_NO"))
        rows = by_cm.get(key, [])
        gnrl = int(m.get("SUPLY_HSHLDCO") or 0)
        spsply = int(m.get("SPSPLY_HSHLDCO") or 0)
        amount = num(m.get("LTTOT_TOP_AMOUNT"))
        area = num(m.get("SUPLY_AR"))

        # 순차 배정에서 마감된 지점에만 경쟁률 숫자가 들어간다.
        closed, rate, total_req = None, None, 0
        seen = {}
        for r in rows:
            rank = int(r.get("SUBSCRPT_RANK_CODE") or 0)
            zone = r.get("RESIDE_SENM")
            seen[(rank, zone)] = r
            total_req += int(num(r.get("REQ_CNT")) or 0)
        for rank, zone in RANK_ORDER:
            r = seen.get((rank, zone))
            if not r:
                continue
            v = num(r.get("CMPET_RATE"))
            if v is not None:
                closed, rate = f"{rank}순위 {zone}", v
                break

        if not rows:
            result = "결과없음"
        elif closed:
            result = "마감"
        else:
            result = "미달"

        types.append({
            "ty": m["HOUSE_TY"],
            "area": round(area, 2) if area else None,
            "gnrl": gnrl, "spsply": spsply, "total": gnrl + spsply,
            "amount": int(amount) if amount else None,
            "pyeong": pyeong_price(amount, area),
            "result": result, "closedAt": closed, "rate": rate,
            "req": total_req,
        })
    return types


def weighted_avg(pairs):
    num_, den = 0.0, 0
    for value, weight in pairs:
        if value and weight:
            num_ += value * weight
            den += weight
    return round(num_ / den) if den else None


def aggregate(data, unsold, loc, today=None):
    today = today or date.today()
    frm = today.replace(year=today.year - YEARS_BACK).isoformat()
    to = today.isoformat()

    models = collections.defaultdict(list)
    for m in data["model"]:
        models[m["HOUSE_MANAGE_NO"]].append(m)
    cmpets = collections.defaultdict(list)
    for c in data["cmpet"]:
        cmpets[c["HOUSE_MANAGE_NO"]].append(c)

    regions = collections.defaultdict(lambda: {"notices": []})
    used = 0
    for n in data["notice"]:
        d = n.get("RCRIT_PBLANC_DE") or ""
        if not (frm <= d <= to):
            continue
        used += 1
        sido, sgg = loc.locate(n.get("HSSPLY_ADRES"),
                               n.get("SUBSCRPT_AREA_CODE_NM"),
                               n.get("HOUSE_NM") or "")
        if not sgg:
            continue
        mno = n["HOUSE_MANAGE_NO"]
        types = build_types(models.get(mno, []), cmpets.get(mno, []))
        supply = sum(t["total"] for t in types)
        rec = {
            "id": mno,
            "name": n.get("HOUSE_NM"),
            "addr": n.get("HSSPLY_ADRES"),
            "noticeDate": d,
            "rceptDate": n.get("RCEPT_BGNDE"),
            "moveIn": n.get("MVN_PREARNGE_YM"),
            "builder": n.get("CNSTRCT_ENTRPS_NM"),
            "kind": n.get("HOUSE_DTL_SECD_NM"),
            "rent": n.get("RENT_SECD_NM"),
            "totalHshld": n.get("TOT_SUPLY_HSHLDCO") or supply,
            "url": n.get("PBLANC_URL"),
            "homepage": n.get("HMPG_ADRES"),
            "speculative": n.get("MDAT_TRGET_AREA_SECD") == "Y",
            "priceCap": n.get("PARCPRC_ULS_AT") == "Y",
            "types": types,
            "avgPyeong": weighted_avg([(t["pyeong"], t["total"]) for t in types]),
            "supply": supply,
            "closedTypes": sum(1 for t in types if t["result"] == "마감"),
            "ratedTypes": sum(1 for t in types if t["result"] != "결과없음"),
        }
        regions[f"{sido}|{sgg}"]["notices"].append(rec)

    # 미분양 붙이기
    base_ym = {}
    for kind, rows in unsold.items():
        for r in rows:
            if r["C2_NM"] in ("계", "합계"):
                continue
            sido = r["C1_NM"]
            key = f"{sido}|{r['C2_NM']}"
            v = num(r.get("DT"))
            regions[key].setdefault("unsold", {})[kind] = int(v) if v is not None else None
            base_ym[kind] = r["PRD_DE"]

    for key, v in regions.items():
        v["notices"].sort(key=lambda x: x["noticeDate"], reverse=True)
        v.setdefault("unsold", {})

    sido_summary = collections.defaultdict(
        lambda: {"total": 0, "after": 0, "notices": 0, "supply": 0, "pyeong": []})
    for key, v in regions.items():
        sido = key.split("|")[0]
        s = sido_summary[sido]
        s["total"] += (v["unsold"].get("total") or 0)
        s["after"] += (v["unsold"].get("after") or 0)
        s["notices"] += len(v["notices"])
        s["supply"] += sum(n["supply"] for n in v["notices"])
        for n in v["notices"]:
            if n["avgPyeong"]:
                s["pyeong"].append((n["avgPyeong"], n["supply"]))
    for s in sido_summary.values():
        s["avgPyeong"] = weighted_avg(s.pop("pyeong"))

    return {
        "meta": {
            "generated": today.isoformat(),
            "noticeFrom": frm, "noticeTo": to,
            "noticeCount": used,
            "unsoldBaseYm": base_ym.get("total"),
            "unsoldAfterBaseYm": base_ym.get("after"),
            "unresolved": len(loc.unresolved),
        },
        "regions": dict(regions),
        "sidoSummary": dict(sido_summary),
    }


# --------------------------------------------------------------- 출력
def write_web(out):
    """첫 화면에 필요한 요약과 시도별 상세를 분리해 저장한다.

    3년치를 한 파일에 담으면 1.4MB가 넘어 모바일 첫 로딩이 느리다.
    지도에 필요한 시군구 요약만 index.json 에 넣고, 공고 상세는
    사용자가 시도를 고른 뒤에 그 시도 파일만 받는다.
    """
    os.makedirs(f"{WEB}/region", exist_ok=True)
    summary = {}
    for key, v in out["regions"].items():
        notices = v["notices"]
        summary[key] = {
            "unsold": v["unsold"].get("total"),
            "after": v["unsold"].get("after"),
            "n": len(notices),
            "supply": sum(x["supply"] for x in notices),
            "pyeong": weighted_avg([(x["avgPyeong"], x["supply"]) for x in notices]),
        }
    json.dump({"meta": out["meta"], "summary": summary,
               "sidoSummary": out["sidoSummary"]},
              open(f"{WEB}/index.json", "w"), ensure_ascii=False,
              separators=(",", ":"))

    by_sido = collections.defaultdict(dict)
    for key, v in out["regions"].items():
        sido, sgg = key.split("|")
        by_sido[sido][sgg] = v
    for sido, payload in by_sido.items():
        json.dump(payload, open(f"{WEB}/region/{SLUG[sido]}.json", "w"),
                  ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------- 실행
def main():
    load_env()
    offline = "--offline" in sys.argv
    os.makedirs(RAW, exist_ok=True)
    os.makedirs(WEB, exist_ok=True)

    if offline:
        data = {k: json.load(open(f"{RAW}/{k}.json"))
                for k in ("notice", "model", "cmpet")}
        unsold = {k: json.load(open(f"{RAW}/unsold_{k}.json"))
                  for k in UNSOLD_TABLES}
    else:
        dk = os.environ.get("DATA_GO_KR_KEY")
        kk = os.environ.get("KOSIS_KEY")
        if not dk or not kk:
            sys.exit("DATA_GO_KR_KEY / KOSIS_KEY 를 .env 나 환경변수에 넣어주세요.")
        print("수집 시작")
        data = fetch_applyhome(dk)
        unsold = fetch_unsold(kk)

    canon = collections.defaultdict(set)
    for r in unsold["total"]:
        if r["C2_NM"] not in ("계", "합계"):
            canon[r["C1_NM"]].add(r["C2_NM"])
    loc = Locator(canon)

    out = aggregate(data, unsold, loc)
    write_web(out)

    m = out["meta"]
    matched = sum(len(v["notices"]) for v in out["regions"].values())
    print(f"\n최근 {YEARS_BACK}년 공고 {m['noticeCount']}건 중 {matched}건 시군구 매칭"
          f" ({matched / max(m['noticeCount'], 1) * 100:.1f}%)")
    print(f"미분양 기준월 전체 {m['unsoldBaseYm']} / 준공후 {m['unsoldAfterBaseYm']}")
    if loc.unresolved:
        print("주소 미해석:")
        for a, h in loc.unresolved[:15]:
            print("   ", h, "|", a)
    idx = os.path.getsize(f"{WEB}/index.json") // 1024
    big = max(os.path.getsize(f"{WEB}/region/{f}")
              for f in os.listdir(f"{WEB}/region")) // 1024
    print(f"web/index.json {idx} KB / 시도 파일 최대 {big} KB")


if __name__ == "__main__":
    main()
