# -*- coding: utf-8 -*-
"""지역별 대장아파트와 평균 시세를 실거래가로 계산한다.

  python etl_daejang.py             # 수집 + 집계 (인증키 필요)
  python etl_daejang.py --offline   # raw/rtms 에 받아둔 것으로 집계만
  python etl_daejang.py --no-geo    # 좌표 조회를 건너뛴다

출력
  web/daejang/index.json          전국·시도·시군구 요약 (지도 색칠용)
  web/daejang/{시도슬러그}.json    시군구별 대장·상위 단지·법정동(서울)·대장후보(서울)
  web/daejang-geo.json            단지 좌표 캐시
  kapt-cache.json                 K-apt 단지 기본정보 캐시 (세대수·사용승인일)

대장 판정
  1) 최근 12개월 매매 실거래(해제·직거래 제외)로 단지별 전용 3.3㎡당 가격을 낸다.
     최근 6개월 거래가 3건 이상이면 그 6개월만, 아니면 12개월 전체의 중위값이다.
  2) 지역 등급별 기준(연식·세대수)을 통과한 단지 중 3.3㎡당 가격 1위가 대장이다.
     브랜드는 판정에 쓰지 않고 표시만 한다. 시세를 이끄는 대단지가 대장이다.
  3) 기준을 통과한 단지가 없으면 단계적으로 완화하고, 완화했다고 표시한다.

공공데이터포털에서 아래 서비스를 각각 활용신청해야 한다 (같은 인증키).
  - 국토교통부_아파트 매매 실거래가 상세 자료     (RTMSDataSvcAptTradeDev)
  - 국토교통부_아파트 분양권전매 실거래가 자료     (RTMSDataSvcSilvTrade) 서울 대장후보용
  - 국토교통부_공동주택 단지 목록제공 서비스       (AptListService)       세대수 매칭용
  - 국토교통부_공동주택 기본 정보제공 서비스       (AptBasisInfoService)  세대수
K-apt 두 서비스가 없으면 세대수를 확인할 수 없어, 거래량을 대단지의
대용 지표로 쓰고 화면에 '세대수 미확인'으로 표시한다.
"""
import collections, json, math, os, re, statistics, sys, time
import urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import lawd
from slug import SLUG

ROOT = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(ROOT, "raw", "rtms")
WEB = os.path.join(ROOT, "web")
OUT = os.path.join(WEB, "daejang")
GEO_CACHE = os.path.join(WEB, "daejang-geo.json")
KAPT_CACHE = os.path.join(ROOT, "kapt-cache.json")
SEED = os.path.join(ROOT, "daejang_seed.json")

API = "https://apis.data.go.kr/1613000"
TRADE = ("RTMSDataSvcAptTradeDev", "getRTMSDataSvcAptTradeDev")
SILV = ("RTMSDataSvcSilvTrade", "getRTMSDataSvcSilvTrade")
# K-apt 서비스는 버전이 자주 바뀐다. 최신부터 차례로 시도한다.
KAPT_LIST = [("AptListService4", "getLegaldongAptList4"),
             ("AptListService3", "getLegaldongAptList3"),
             ("AptListService2", "getLegaldongAptList")]
KAPT_BASIC = [("AptBasisInfoServiceV5", "getAphusBassInfoV5"),
              ("AptBasisInfoServiceV4", "getAphusBassInfoV4"),
              ("AptBasisInfoServiceV3", "getAphusBassInfoV3")]

MONTHS = 12            # 조회 기간 (이번 달 포함 13개 달력월을 받아 365일로 자른다)
RECENT_DAYS = 183      # 최근 거래가 충분하면 이 기간만 쓴다
REFRESH_MONTHS = 3     # 최근 이만큼은 매번 새로 받는다 (지연 신고·해제 반영)
PY = 3.3058            # ㎡ -> 평
SILV_SIDO = {"서울"}   # 분양권 거래를 받을 시도 (대장후보용)
DONG_SIDO = {"서울"}   # 법정동 단위 대장까지 계산할 시도
MAX_CALLS = int(os.environ.get("DAEJANG_MAX_CALLS", "9000"))
MAX_GEOCODE = int(os.environ.get("DAEJANG_MAX_GEOCODE", "600"))

# 10대 건설사 주력 브랜드. 판정이 아니라 표시용이다.
BRANDS = [
    ("래미안", "삼성물산"), ("디에이치", "현대건설"), ("힐스테이트", "현대건설·현대엔지니어링"),
    ("푸르지오", "대우건설"), ("써밋", "대우건설"), ("아크로", "DL이앤씨"),
    ("e편한세상", "DL이앤씨"), ("e-편한세상", "DL이앤씨"), ("이편한세상", "DL이앤씨"),
    ("자이", "GS건설"), ("더샵", "포스코이앤씨"), ("오티에르", "포스코이앤씨"),
    ("롯데캐슬", "롯데건설"), ("르엘", "롯데건설"), ("SK뷰", "SK에코플랜트"),
    ("SKVIEW", "SK에코플랜트"), ("에스케이뷰", "SK에코플랜트"), ("드파인", "SK에코플랜트"),
    ("아이파크", "HDC현대산업개발"), ("IPARK", "HDC현대산업개발"),
]
HIGH_END = ("디에이치", "아크로", "써밋", "르엘", "오티에르", "드파인")

# 지역 등급별 대장 기준 (연식 이내 년, 세대수 이상). 첫 단계가 정규 기준이고
# 뒤는 후보가 없을 때의 완화 단계다.
METRO = {"경기", "인천", "세종", "부산", "대구", "대전", "울산"}
GWANGJU_GU = {"동구", "서구", "남구", "북구", "광산구"}   # 전남광주 중 옛 광주


def tiers_for(sido, sgg, level):
    if sido == "서울" and level == "dong":
        return [(10, 1000), (15, 1000), (10, 500), (15, 500), (20, 300)]
    if sido == "서울":
        return [(10, 1000), (15, 1000), (15, 500)]
    if sido in METRO or (sido == "전남광주" and sgg in GWANGJU_GU):
        return [(10, 1000), (10, 700), (15, 500), (20, 300)]
    return [(10, 500), (15, 500), (20, 300)]


def tier_text(t):
    return f"{t[0]}년 이내·{t[1]:,}세대 이상"


# ------------------------------------------------------------------ 공통
def load_env():
    p = os.path.join(ROOT, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)


def scrub(text):
    return re.sub(r"((?:serviceKey|apiKey)=)[^&\s]*", r"\1***", str(text))


def jload(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def jdump(obj, path, pretty=False):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if pretty:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        else:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"{code} {msg}")
        self.code, self.msg = str(code), str(msg)

    @property
    def fatal(self):
        """인증키·활용신청·서비스 없음처럼 다시 해도 소용없는 오류."""
        s = f"{self.code} {self.msg}".upper()
        return any(k in s for k in ("NOT_REGISTERED", "NO_OPENAPI", "SERVICE_KEY",
                                    "UNREGISTERED", "DEADLINE", "ACCESS_DENIED",
                                    "SERVICE ACCESS DENIED", "UNAUTHORIZED",
                                    "HTTP 401", "HTTP 403", "HTTP 404")) \
            or self.code in ("12", "20", "30", "31", "32", "33")

    @property
    def quota(self):
        s = f"{self.code} {self.msg}".upper()
        return "LIMITED_NUMBER" in s or self.code == "22" or "HTTP 429" in s


def parse_api(text):
    """공공데이터포털 응답(XML 또는 JSON) -> (items[list[dict]], totalCount).

    오류 응답이면 ApiError 를 던진다. 서비스마다 헤더 모양이 조금씩 달라
    알려진 형태를 모두 받는다.
    """
    text = text.strip()
    if text.startswith("{"):
        j = json.loads(text)
        r = j.get("response", j)
        head = r.get("header", {}) or {}
        code = str(head.get("resultCode", "00"))
        if code not in ("00", "000", "0"):
            raise ApiError(code, head.get("resultMsg", ""))
        body = r.get("body", {}) or {}
        it = body.get("items", body.get("item"))
        if isinstance(it, dict):
            it = it.get("item", it)
        if not it:
            it = []
        if isinstance(it, dict):
            it = [it]
        return it, int(body.get("totalCount") or len(it))
    root = ET.fromstring(text)
    if root.tag == "OpenAPI_ServiceResponse":
        code = root.findtext(".//returnReasonCode") or "99"
        msg = root.findtext(".//returnAuthMsg") or root.findtext(".//errMsg") or ""
        raise ApiError(code, msg)
    code = root.findtext("./header/resultCode") or root.findtext(".//resultCode") or "00"
    msg = root.findtext("./header/resultMsg") or root.findtext(".//resultMsg") or ""
    if code not in ("00", "000", "0"):
        raise ApiError(code, msg)
    items = []
    for it in root.iter("item"):
        items.append({c.tag: (c.text or "").strip() for c in it})
    total = root.findtext(".//totalCount")
    return items, int(total) if total and total.isdigit() else len(items)


class Client:
    """호출 수를 세고, 일시 오류는 다시 시도하는 얇은 래퍼."""

    def __init__(self, key, max_calls=MAX_CALLS):
        # 공공데이터포털 키는 Encoding/Decoding 두 표기가 있다. urlencode 가
        # 한 번 더 인코딩하므로 Decoding 표기로 맞춘다. Encoding 키를 그대로
        # 넣으면 apis.data.go.kr 은 SERVICE_KEY_IS_NOT_REGISTERED_ERROR 를 낸다
        # (청약홈 api.odcloud.kr 은 둘 다 받아줘서 같은 키로도 차이가 난다).
        key = (key or "").strip()
        if "%" in key:
            key = urllib.parse.unquote(key)
        self.key, self.calls, self.max_calls = key, 0, max_calls
        self.exhausted = False

    def get(self, svc, op, params, tries=4):
        if self.calls >= self.max_calls:
            self.exhausted = True
            raise ApiError("BUDGET", f"이번 실행의 호출 상한 {self.max_calls}회 도달")
        q = urllib.parse.urlencode({"serviceKey": self.key, **params})
        url = f"{API}/{svc}/{op}?{q}"
        last = None
        for i in range(tries):
            self.calls += 1
            try:
                req = urllib.request.Request(url, headers={"Accept": "*/*"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    return parse_api(r.read().decode("utf-8", "replace"))
            except ApiError as e:
                if e.fatal or e.quota:
                    raise
                last = e
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                last = ApiError(f"HTTP {e.code}", body)
                if e.code in (401, 403, 404, 429):
                    raise last
            except Exception as e:                       # 네트워크 일시 오류
                last = ApiError(type(e).__name__, scrub(e))
            time.sleep(2 * (i + 1))
        raise last


# ------------------------------------------------------------------ 실거래 수집
def month_list(today):
    y, m = today.year, today.month
    out = []
    for _ in range(MONTHS + 1):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out                        # 최근 달부터


def fetch_month(cli, spec, code, ym):
    rows, page = [], 1
    while True:
        items, total = cli.get(spec[0], spec[1], {
            "LAWD_CD": code, "DEAL_YMD": ym, "pageNo": page, "numOfRows": 1000})
        rows += items
        if len(rows) >= total or not items:
            return rows
        page += 1


def collect(cli, kind, codes, months, offline):
    """raw/rtms/{kind}/{ym}/{code}.json 캐시를 채우고 전체를 돌려준다."""
    spec = TRADE if kind == "trade" else SILV
    out, fetched, failed, streak = [], 0, 0, 0
    stop_reason = None
    for mi, ym in enumerate(months):
        for code in codes:
            path = os.path.join(RAW, kind, ym, f"{code}.json")
            fresh = os.path.exists(path) and (mi >= REFRESH_MONTHS or offline)
            if not fresh and not offline and not stop_reason:
                try:
                    rows = fetch_month(cli, spec, code, ym)
                    jdump(rows, path)
                    fetched += 1
                    streak = 0
                    time.sleep(0.05)
                except ApiError as e:
                    failed += 1
                    streak += 1
                    if streak >= 20:          # 네트워크가 통째로 안 될 때 시간 낭비를 막는다
                        e = ApiError("STREAK", f"연속 {streak}회 실패, 마지막: {e}")
                    if e.fatal or e.quota or e.code in ("BUDGET", "STREAK"):
                        stop_reason = e
                        print(f"  [{kind}] 수집 중단: {scrub(e)}", flush=True)
                    else:
                        print(f"  [{kind}] {code} {ym} 실패: {scrub(e)}", flush=True)
            if os.path.exists(path):
                for r in jload(path, []):
                    r["_code"] = code
                    out.append(r)
        print(f"  [{kind}] {ym} 누적 {len(out):,}건 (새로 받음 {fetched}, 실패 {failed})",
              flush=True)
    return out, stop_reason


# ------------------------------------------------------------------ 정규화
def to_int(v):
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def to_float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def norm_name(s):
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s or "")
    s = s.replace("아파트", "").replace("APT", "").replace("apt", "")
    s = re.sub(r"제(\d+)단지", r"\1단지", s)
    s = re.sub(r"[\s\-·.,'\"()&]", "", s)
    return s.lower()


def brand_of(name):
    n = (name or "").replace(" ", "")
    nl = n.lower()
    for b, co in BRANDS:
        if b.lower() in nl:
            return b, co
    return None, None


def clean_trades(rows, today, kind):
    """API 행 -> 정리된 거래. 해제는 버리고, 직거래는 표시만 해 둔다."""
    since = today - timedelta(days=365)
    seen, out = set(), []
    for r in rows:
        code = r["_code"]
        if code not in lawd.CODES:
            continue
        y, m, d = to_int(r.get("dealYear")), to_int(r.get("dealMonth")), to_int(r.get("dealDay"))
        amt, ar = to_int(r.get("dealAmount")), to_float(r.get("excluUseAr"))
        if not (y and m and d and amt and ar):
            continue
        try:
            dt = date(y, m, d)
        except ValueError:
            continue
        if dt < since or dt > today:
            continue
        if (r.get("cdealType") or "").strip() or (r.get("cdealDay") or "").strip():
            continue                                   # 해제된 거래
        umd = (r.get("umdNm") or "").strip()
        name = (r.get("aptNm") or "").strip()
        jibun = (r.get("jibun") or "").strip()
        fl = to_int(r.get("floor"))
        sido, sgg = lawd.app_region(code, umd)
        # 옛 코드·새 코드로 같은 거래가 두 번 잡히는 것을 지운다.
        k = (sido, sgg, umd, jibun, norm_name(name), (r.get("aptDong") or "").strip(),
             dt, amt, round(ar, 2), fl, (r.get("ownershipGbn") or "").strip())
        if k in seen:
            continue
        seen.add(k)
        road = (r.get("roadNm") or "").strip()
        bon = (r.get("roadNmBonbun") or "").lstrip("0")
        bu = (r.get("roadNmBubun") or "").lstrip("0")
        out.append({
            "sido": sido, "sgg": sgg, "code": code, "umd": umd,
            "umdCd": (r.get("umdCd") or "").strip(), "sggCd": (r.get("sggCd") or code).strip(),
            "jibun": jibun, "name": name, "by": to_int(r.get("buildYear")),
            "date": dt, "amt": amt, "ar": ar, "fl": fl,
            "ppy": None,                     # apply_supply() 가 공급면적 기준으로 채운다
            "direct": (r.get("dealingGbn") or "").strip() == "직거래",
            "road": f"{road} {bon}{'-' + bu if bu else ''}".strip() if road and bon else "",
            "own": (r.get("ownershipGbn") or "").strip() if kind == "silv" else "",
        })
    return out


# ------------------------------------------------------------------ 공급면적 환산
# 실거래 자료에는 전용면적만 있다. 시세를 공급면적 평당가로 보이려면
# 공급/전용 비율이 필요하다.
#   1) 청약 공고(web/region)에 주택형별 공급면적이 있는 단지는 그 비율을 쓴다.
#   2) 나머지는 평형별 통상 비율로 환산한다. 기준점은 시장에서 흔히 쓰는
#      평형 표기다: 전용 59㎡ ≈ 25평형, 84㎡ ≈ 34평형, 114㎡ ≈ 44평형.
#      단지·연식마다 전용률이 달라 실제 공급면적과 몇 % 차이 날 수 있다.
SUPPLY_POINTS = [(40, 1.42), (59.9, 1.38), (84.9, 1.32), (114.9, 1.27),
                 (135, 1.25), (200, 1.22)]


def default_ratio(ar):
    pts = SUPPLY_POINTS
    if ar <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if ar <= x1:
            return y0 + (y1 - y0) * (ar - x0) / (x1 - x0)
    return pts[-1][1]


def notice_ratios(notices_by_sgg):
    """청약 공고 -> {(시도, 시군구): [(정규화 단지명, [(전용, 공급비율)])]}"""
    out = collections.defaultdict(list)
    for key, ns in notices_by_sgg.items():
        for n in ns:
            pairs = []
            for t in n.get("types", []):
                m = re.match(r"\s*(\d+(?:\.\d+)?)", str(t.get("ty") or ""))
                ex, sup = (float(m.group(1)) if m else None), t.get("area")
                if ex and sup and 1.05 < sup / ex < 1.8:
                    pairs.append((ex, sup / ex))
            if pairs:
                out[key].append((norm_name(n.get("name")), pairs))
    return out


def apply_supply(trades, ratios):
    """거래마다 공급면적(sup)과 공급 3.3㎡당 가격(ppy)을 채운다."""
    memo, hit = {}, 0
    for t in trades:
        k = (t["sido"], t["sgg"], norm_name(t["name"]))
        if k not in memo:
            best, score = None, 0
            for nn, pairs in ratios.get((t["sido"], t["sgg"]), []):
                sc = name_score(nn, k[2])
                if sc > score:
                    best, score = pairs, sc
            memo[k] = best if score >= 0.85 else None
        pairs = memo[k]
        if pairs:
            # 같은 평형대(전용 ±3㎡)의 공고 비율, 없으면 공고 전체 중위
            near = [r for ex, r in pairs if abs(ex - t["ar"]) <= 3]
            ratio, src = (median(near) if near else median([r for _, r in pairs])), "공고"
            hit += 1
        else:
            ratio, src = default_ratio(t["ar"]), "환산"
        t["sup"] = t["ar"] * ratio
        t["supSrc"] = src
        t["ppy"] = t["amt"] / (t["sup"] / PY)
    return hit


# ------------------------------------------------------------------ 단지 집계
def median(xs):
    return statistics.median(xs) if xs else None


def price_block(trades, today):
    """단지(또는 지역)의 가격 요약. 직거래는 가격 판정에서 뺀다."""
    valid = [t for t in trades if not t["direct"]]
    if not valid:
        return None
    recent_since = today - timedelta(days=RECENT_DAYS)
    recent = [t for t in valid if t["date"] >= recent_since]
    use = recent if len(recent) >= 3 else valid
    ppy = median([t["ppy"] for t in use])
    t84 = [t for t in use if 80 <= t["ar"] < 90] or [t for t in valid if 80 <= t["ar"] < 90]
    p84 = median([t["amt"] for t in t84])
    last = max(valid, key=lambda t: (t["date"], t["amt"]))
    top = max(valid, key=lambda t: t["amt"])
    return {
        "ppy": round(ppy), "p84": round(p84) if p84 else round(ppy * 84.9 * default_ratio(84.9) / PY),
        "p84est": not t84, "n": len(valid), "nRecent": len(recent),
        "basis": "6m" if use is recent else "12m",
        "last": {"d": last["date"].isoformat(), "amt": last["amt"],
                 "ar": round(last["ar"], 1), "fl": last["fl"]},
        "max": {"d": top["date"].isoformat(), "amt": top["amt"],
                "ar": round(top["ar"], 1), "fl": top["fl"]},
    }


def build_complexes(trades, today):
    groups = collections.defaultdict(list)
    for t in trades:
        groups[(t["sido"], t["sgg"], t["umd"], t["jibun"], norm_name(t["name"]))].append(t)
    cx = []
    for (sido, sgg, umd, jibun, nn), ts in groups.items():
        pb = price_block(ts, today)
        if not pb:
            continue
        ts.sort(key=lambda t: t["date"], reverse=True)
        t0 = ts[0]
        bys = [t["by"] for t in ts if t["by"]]
        by = collections.Counter(bys).most_common(1)[0][0] if bys else None
        b, co = brand_of(t0["name"])
        sup_src = "공고" if any(t["supSrc"] == "공고" for t in ts) else "환산"
        cx.append({
            "id": f"{t0['sggCd']}-{t0['umdCd']}-{jibun}-{nn}"[:80],
            "sido": sido, "sgg": sgg, "code": t0["code"], "gu": lawd.gu_label(t0["code"]),
            "dong": umd, "umdCd": t0["umdCd"], "sggCd": t0["sggCd"],
            "jibun": jibun, "road": next((t["road"] for t in ts if t["road"]), ""),
            "name": t0["name"], "nn": nn, "by": by,
            "age": (today.year - by) if by else None,
            "brand": b, "brandCo": co, "hi": bool(b and b in HIGH_END),
            "supSrc": sup_src,
            **pb,
        })
    return cx


# ------------------------------------------------------------------ 세대수 (K-apt)
class Kapt:
    def __init__(self, cli, offline):
        self.cli, self.offline = cli, offline
        self.cache = jload(KAPT_CACHE, {"basic": {}, "list": {}})
        self.cache.setdefault("basic", {})
        self.cache.setdefault("list", {})
        self.list_path = self.basic_path = None
        self.unavailable = None          # 사유 문자열
        self.calls = 0

    def save(self):
        jdump(self.cache, KAPT_CACHE)

    def _try_paths(self, paths, attr, params):
        """서비스 경로를 최신부터 시도해 되는 것을 기억한다."""
        if getattr(self, attr):
            svc, op = getattr(self, attr)
            return self.cli.get(svc, op, params)
        last = None
        for svc, op in paths:
            try:
                res = self.cli.get(svc, op, params)
                setattr(self, attr, (svc, op))
                print(f"  K-apt 경로 확인: {svc}/{op}", flush=True)
                return res
            except ApiError as e:
                last = e
                if e.quota or e.code == "BUDGET":
                    raise
        raise last

    def complexes_in(self, bjd):
        """법정동(10자리) 안의 K-apt 단지 목록 [{code, name}]."""
        hit = self.cache["list"].get(bjd)
        if hit is not None and (time.time() - hit.get("t", 0)) < 30 * 86400:
            return hit["items"]
        if self.offline or self.unavailable:
            return hit["items"] if hit else []
        try:
            items, _ = self._try_paths(KAPT_LIST, "list_path",
                                       {"bjdCode": bjd, "pageNo": 1, "numOfRows": 1000})
        except ApiError as e:
            if e.fatal:
                self.unavailable = f"단지 목록 서비스 사용 불가 ({e.code} {e.msg[:60]})"
                print("  [K-apt] " + self.unavailable, flush=True)
            elif e.quota or e.code == "BUDGET":
                self.unavailable = f"호출 한도 도달 ({e.code})"
            return hit["items"] if hit else []
        out = [{"code": it.get("kaptCode"), "name": it.get("kaptName", "")}
               for it in items if it.get("kaptCode")]
        self.cache["list"][bjd] = {"t": int(time.time()), "items": out}
        self.calls += 1
        return out

    def basic(self, kcode):
        hit = self.cache["basic"].get(kcode)
        if hit is not None:
            return hit
        if self.offline or self.unavailable:
            return None
        try:
            items, _ = self._try_paths(KAPT_BASIC, "basic_path", {"kaptCode": kcode})
        except ApiError as e:
            if e.fatal:
                self.unavailable = f"기본정보 서비스 사용 불가 ({e.code} {e.msg[:60]})"
                print("  [K-apt] " + self.unavailable, flush=True)
            elif e.quota or e.code == "BUDGET":
                self.unavailable = f"호출 한도 도달 ({e.code})"
            return None
        it = items[0] if items else {}
        rec = {
            "name": it.get("kaptName", ""),
            "hh": to_int(it.get("kaptdaCnt") or it.get("hoCnt")),
            "use": (it.get("kaptUsedate") or "")[:8],
            "bc": it.get("kaptBcompany", ""),
            "addr": it.get("doroJuso") or it.get("kaptAddr") or "",
        }
        self.cache["basic"][kcode] = rec
        self.calls += 1
        return rec


def alt_sgg_codes(code):
    """개편 전후로 바뀐 시군구 코드 짝 (K-apt 가 옛 코드를 쓰는 경우 대비)."""
    sido, name = lawd.CODES[code]
    out = [c for c, (s, n) in lawd.CODES.items() if c != code and s == sido and n == name]
    extra = {"28125": ["28110", "28140"], "28155": ["28110"], "28275": ["28260"],
             "28290": ["28260"], "28110": ["28125", "28155"], "28140": ["28125"],
             "28260": ["28275", "28290"]}
    return out + extra.get(code, [])


def name_score(a, b):
    if not a or not b:
        return 0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85 if min(len(a), len(b)) >= 3 else 0.5
    ga = {a[i:i + 2] for i in range(len(a) - 1)}
    gb = {b[i:i + 2] for i in range(len(b) - 1)}
    if not ga or not gb:
        return 0
    return len(ga & gb) / len(ga | gb)


class Households:
    """단지 -> 세대수. 시드(손으로 확인한 값)를 먼저 보고, 없으면 K-apt."""

    def __init__(self, kapt, seed):
        self.kapt, self.memo = kapt, {}
        self.seed = []
        for s in seed:
            keys = {norm_name(s["name"])} | {norm_name(a) for a in s.get("alias", [])}
            self.seed.append((s, {k for k in keys if k}))

    def seed_for(self, c):
        for s, keys in self.seed:
            if s.get("sido") != c["sido"] or s.get("gu") != c["sgg"]:
                continue
            if any(name_score(k, c["nn"]) >= 0.85 for k in keys):
                return s
        return None

    def get(self, c):
        if c["id"] in self.memo:
            return self.memo[c["id"]]
        res = None
        s = self.seed_for(c)
        if s:
            res = {"hh": s["households"], "src": "조사", "bc": s.get("builder"),
                   "srcUrl": s.get("source")}
        elif self.kapt:
            res = self._kapt(c)
        self.memo[c["id"]] = res
        return res

    def _kapt(self, c):
        umd = (c.get("umdCd") or "").zfill(5)
        if not umd.strip("0"):
            return None
        best, score = None, 0
        for sc in [c["sggCd"]] + alt_sgg_codes(c["code"]):
            for it in self.kapt.complexes_in(sc + umd):
                sc_ = name_score(norm_name(it["name"]), c["nn"])
                if sc_ > score:
                    best, score = it, sc_
            if best and score >= 0.6:
                break
        if not best or score < 0.6:
            return None
        b = self.kapt.basic(best["code"])
        if not b or not b.get("hh"):
            return None
        return {"hh": b["hh"], "src": "K-apt", "bc": b.get("bc"),
                "use": b.get("use"), "kapt": best["code"]}


# ------------------------------------------------------------------ 대장 판정
def pick_leader(cx, tiers, min_n, hhs, proxy):
    """기준 단계를 차례로 적용해 3.3㎡당 가격 1위 대단지를 고른다.

    proxy: 세대수를 확인할 수 없을 때(K-apt 미신청) 거래량으로 대신한다.
    """
    pool_all = [c for c in cx if c["n"] >= min_n and c["age"] is not None]
    if proxy:
        ns = sorted(c["n"] for c in pool_all)
        cut = max(8, ns[int(len(ns) * 0.8)] if ns else 8)
    for i, (age, need) in enumerate(tiers):
        pool = sorted((c for c in pool_all if c["age"] <= age),
                      key=lambda c: -c["ppy"])
        looked = 0
        for c in pool:
            h = hhs.get(c)
            if h and h["hh"]:
                if h["hh"] >= need:
                    return c, i, h
            elif proxy and c["n"] >= cut:
                return c, i, {"hh": None, "src": "거래량 대용", "proxyN": cut}
            looked += 1
            if looked >= 25:
                break
    return None, None, None


def pub(c, h=None, today=None):
    """화면용 단지 레코드."""
    out = {k: c[k] for k in ("name", "gu", "dong", "jibun", "road", "by", "age",
                             "brand", "brandCo", "hi", "supSrc", "ppy", "p84", "p84est",
                             "n", "nRecent", "basis", "last", "max")}
    if h:
        out["hh"] = h.get("hh")
        out["hhSrc"] = h.get("src")
        if h.get("bc"):
            out["builder"] = h["bc"]
        if h.get("srcUrl"):
            out["srcUrl"] = h["srcUrl"]
        if h.get("proxyN"):
            out["proxyN"] = h["proxyN"]
    out["_id"] = c["id"]
    return out


def region_stats(trades, today):
    valid = [t for t in trades if not t["direct"]]
    if not valid:
        return None
    t84 = [t["amt"] for t in valid if 80 <= t["ar"] < 90]
    return {"avg": round(sum(t["ppy"] for t in valid) / len(valid)),
            "med": round(median([t["ppy"] for t in valid])),
            "p84": round(median(t84)) if t84 else None,
            "n": len(valid), "n84": len(t84)}


def decide(cx, trades, sido, sgg, level, hhs, proxy, today, min_n):
    st = region_stats(trades, today)
    if not st:
        return None
    tiers = tiers_for(sido, sgg, level)
    c, i, h = pick_leader(cx, tiers, min_n, hhs, proxy)
    out = {"stats": st, "tiers": [tier_text(t) for t in tiers]}
    if c:
        L = pub(c, h)
        L["tier"] = i
        L["tierText"] = tier_text(tiers[i])
        L["lead"] = round(c["ppy"] / st["avg"], 2) if st["avg"] else None
        rank = sorted(cx, key=lambda x: -x["ppy"])
        L["rank"] = 1 + next((k for k, x in enumerate(rank) if x["id"] == c["id"]), 0)
        L["of"] = len(rank)
        out["leader"] = L
    # 기준과 상관없이 가장 비싼 단지 (재건축 기대 구축 등). 대장과 다를 때만 보인다.
    top_any = [x for x in cx if x["n"] >= min_n]
    if top_any:
        t = max(top_any, key=lambda x: x["ppy"])
        if not c or t["id"] != c["id"]:
            out["top"] = pub(t, hhs.get(t) if t.get("age") is not None and t["age"] <= 20 else None)
    return out


# ------------------------------------------------------------------ 대장후보 (서울 미준공)
def candidates(seed, silv, notices_by_sgg, today):
    """서울 미준공·입주초기 대단지. 분양권·입주권 거래가 있으면 시세를 붙인다."""
    def months_since(ym):
        y, m = int(ym[:4]), int(ym[5:7])
        return (today.year - y) * 12 + today.month - m

    by_sgg = collections.defaultdict(list)
    for t in silv:
        by_sgg[(t["sido"], t["sgg"])].append(t)
    out = collections.defaultdict(list)
    used_silv = set()
    for s in seed:
        if s.get("status") == "입주완료":
            continue
        if s.get("moveIn") and months_since(s["moveIn"]) > 3:
            continue                            # 입주 석 달이 지나면 매매 단지로 본다
        keys = {norm_name(s["name"])} | {norm_name(a) for a in s.get("alias", [])}
        keys = {k for k in keys if k}
        mine = [t for t in by_sgg[(s["sido"], s["gu"])]
                if any(name_score(k, norm_name(t["name"])) >= 0.85 for k in keys)]
        for t in mine:
            used_silv.add(id(t))
        rec = {"name": s["name"], "gu": s["gu"], "dong": s["dong"],
               "hh": s["households"], "builder": s.get("builder"),
               "moveIn": s.get("moveIn"), "status": s.get("status"),
               "note": s.get("note"), "srcUrl": s.get("source")}
        b, _ = brand_of(s["name"])
        rec["brand"], rec["hi"] = b, bool(b and b in HIGH_END)
        pb = price_block(mine, today) if mine else None
        if pb:
            rec["silv"] = {k: pb[k] for k in ("ppy", "p84", "p84est", "n", "last", "max")}
            owns = collections.Counter(t["own"] for t in mine if t["own"])
            rec["silv"]["own"] = "·".join(f"{k}{v}" for k, v in owns.most_common()) or None
        # 청약 공고에서 84형 분양가
        for n in notices_by_sgg.get((s["sido"], s["gu"]), []):
            if any(name_score(k, norm_name(n.get("name"))) >= 0.85 for k in keys):
                t84 = [t for t in n.get("types", []) if str(t.get("ty", "")).startswith("084")]
                if t84 and t84[0].get("amount"):
                    rec["sale84"] = t84[0]["amount"]
                    rec["saleDate"] = n.get("noticeDate")
                break
        out[(s["sido"], s["gu"])].append(rec)
    # 시드에 없는 분양권 거래 단지 중 10대 브랜드는 참고로 붙인다
    extra = collections.defaultdict(list)
    for t in silv:
        if id(t) in used_silv:
            continue
        b, _ = brand_of(t["name"])
        if b:
            extra[(t["sido"], t["sgg"], t["umd"], norm_name(t["name"]))].append(t)
    for (sido, sgg, umd, _), ts in extra.items():
        if len(ts) < 3:
            continue
        pb = price_block(ts, today)
        if not pb:
            continue
        b, _ = brand_of(ts[0]["name"])
        out[(sido, sgg)].append({
            "name": ts[0]["name"], "gu": sgg, "dong": umd, "hh": None,
            "status": "분양권 거래", "brand": b, "hi": b in HIGH_END, "auto": True,
            "silv": {k: pb[k] for k in ("ppy", "p84", "p84est", "n", "last", "max")},
        })
    for v in out.values():
        v.sort(key=lambda r: -((r.get("silv") or {}).get("ppy") or 0))
    return out


# ------------------------------------------------------------------ 좌표
def geocode_all(recs, budget):
    """대장·후보 단지 좌표. 카카오 키가 있으면 카카오, 없으면 Nominatim."""
    cache = jload(GEO_CACHE, {})
    kakao = os.environ.get("KAKAO_REST_KEY")
    done = 0
    for r in recs:
        q = r["_q"]
        if q["key"] in cache:
            r["loc"] = cache[q["key"]]
            continue
        if done >= budget:
            continue
        hit = None
        for kind, text in q["tries"]:
            hit = kakao_lookup(kakao, text, kind) if kakao else osm_lookup(text)
            if not kakao:
                time.sleep(1.1)
            if hit:
                hit = hit + [kind]
                break
        cache[q["key"]] = hit
        r["loc"] = hit
        done += 1
        if done % 25 == 0:
            jdump(cache, GEO_CACHE)
            print(f"  좌표 {done}건 조회", flush=True)
    jdump(cache, GEO_CACHE)
    return done


def kakao_lookup(key, text, kind):
    ep = "address" if kind != "name" else "keyword"
    url = f"https://dapi.kakao.com/v2/local/search/{ep}.json?" + urllib.parse.urlencode(
        {"query": text, "size": 1})
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"KakaoAK {key}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
        doc = (d.get("documents") or [None])[0]
        return [round(float(doc["y"]), 5), round(float(doc["x"]), 5)] if doc else None
    except Exception:
        return None


def osm_lookup(text):
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": text, "format": "json", "limit": 1, "countrycodes": "kr"})
    for i in range(2):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "apt-cheongyak/1.0 (daejang viewer)"})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            return [round(float(d[0]["lat"]), 5), round(float(d[0]["lon"]), 5)] if d else None
        except Exception:
            time.sleep(3)
    return None


SIDO_FULL = {
    "서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시", "인천": "인천광역시",
    "대전": "대전광역시", "울산": "울산광역시", "세종": "세종특별자치시", "경기": "경기도",
    "강원": "강원특별자치도", "충북": "충청북도", "충남": "충청남도", "전북": "전북특별자치도",
    "전남광주": "전남광주통합특별시", "경북": "경상북도", "경남": "경상남도", "제주": "제주특별자치도",
}


def geo_query(sido, rec):
    full = SIDO_FULL.get(sido, sido)
    gu = rec.get("gu") or ""
    gu_txt = "" if sido == "세종" else gu
    tries = []
    if rec.get("road"):
        tries.append(("road", f"{full} {gu_txt} {rec['road']}".replace("  ", " ")))
    if rec.get("jibun"):
        tries.append(("jibun", f"{full} {gu_txt} {rec['dong']} {rec['jibun']}".replace("  ", " ")))
    tries.append(("name", f"{gu_txt} {rec['dong']} {rec['name']}".strip()))
    tries.append(("dong", f"{full} {gu_txt} {rec['dong']}".replace("  ", " ")))
    key = f"{sido}|{gu}|{rec['dong']}|{rec.get('jibun') or rec['name']}"
    return {"key": key, "tries": tries}


# ------------------------------------------------------------------ 실행
def load_notices():
    out = collections.defaultdict(list)
    d = os.path.join(WEB, "region")
    for sido, slug in SLUG.items():
        p = os.path.join(d, f"{slug}.json")
        if not os.path.exists(p):
            continue
        for sgg, v in jload(p, {}).items():
            out[(sido, sgg)] = v.get("notices", [])
    return out


def main(argv=None, today=None, client=None):
    argv = sys.argv[1:] if argv is None else argv
    load_env()
    offline = "--offline" in argv
    today = today or date.today()
    months = month_list(today)
    key = os.environ.get("DATA_GO_KR_KEY")
    if not offline and not key and client is None:
        sys.exit("DATA_GO_KR_KEY 를 .env 나 환경변수에 넣어주세요.")
    cli = client or Client(key)

    codes = list(lawd.CODES)
    print(f"실거래 수집: 시군구 코드 {len(codes)}개 × {len(months)}개월", flush=True)
    trade_rows, trade_stop = collect(cli, "trade", codes, months, offline)
    if trade_stop and not trade_rows:
        sys.exit("아파트 매매 실거래가 상세 자료 API를 쓸 수 없습니다: "
                 f"{scrub(trade_stop)}\n공공데이터포털에서 활용신청 여부를 확인하세요.")
    silv_codes = [c for c in codes if lawd.CODES[c][0] in SILV_SIDO]
    silv_rows, silv_stop = collect(cli, "silv", silv_codes, months, offline)

    trades = clean_trades(trade_rows, today, "trade")
    silv = clean_trades(silv_rows, today, "silv")
    notices = load_notices()
    ratios = notice_ratios(notices)
    h1, h2 = apply_supply(trades, ratios), apply_supply(silv, ratios)
    print(f"공급면적: 청약 공고 비율 적용 매매 {h1:,}건 · 분양권 {h2:,}건, 나머지는 평형별 환산")
    print(f"매매 {len(trades):,}건 · 분양권/입주권 {len(silv):,}건 (최근 365일, 해제 제외)")

    cx = build_complexes(trades, today)
    print(f"단지 {len(cx):,}개")

    seed = jload(SEED, {"items": []})["items"]
    kapt = Kapt(cli, offline)
    hhs = Households(kapt, seed)

    by_sgg_t = collections.defaultdict(list)
    for t in trades:
        by_sgg_t[(t["sido"], t["sgg"])].append(t)
    by_sgg_c = collections.defaultdict(list)
    for c in cx:
        by_sgg_c[(c["sido"], c["sgg"])].append(c)

    regions = {}
    for (sido, sgg), ts in sorted(by_sgg_t.items()):
        # K-apt 를 못 쓰면(미신청·한도) 거래량을 대단지 대용 지표로 쓴다.
        proxy = bool(kapt.unavailable)
        r = decide(by_sgg_c[(sido, sgg)], ts, sido, sgg, "sgg", hhs, proxy, today, 3)
        if not r:
            continue
        cs = sorted(by_sgg_c[(sido, sgg)], key=lambda c: -c["ppy"])
        new = [c for c in cs if c["age"] is not None and c["age"] <= 10 and c["n"] >= 3][:6]
        r["newTop"] = [pub(c, hhs.get(c)) for c in new]
        if sido in DONG_SIDO:
            dts = collections.defaultdict(list)
            for t in ts:
                dts[t["umd"]].append(t)
            dcx = collections.defaultdict(list)
            for c in by_sgg_c[(sido, sgg)]:
                dcx[c["dong"]].append(c)
            dongs = []
            for dong, dt in dts.items():
                d = decide(dcx[dong], dt, sido, sgg, "dong", hhs, proxy, today, 2)
                if d:
                    d["dong"] = dong
                    dongs.append(d)
            dongs.sort(key=lambda d: -d["stats"]["avg"])
            r["dongs"] = dongs
        regions[(sido, sgg)] = r
        if len(regions) % 20 == 0:
            kapt.save()
            print(f"  {len(regions)}개 지역 판정 (API 호출 {cli.calls:,}회)", flush=True)
    kapt.save()

    cand = candidates(seed, silv, notices, today)
    for (sido, sgg), lst in cand.items():
        if (sido, sgg) in regions:
            regions[(sido, sgg)]["cand"] = lst

    # 좌표: 대장·전체 최고가·후보·법정동 대장
    if "--no-geo" not in argv:
        recs = []
        for (sido, sgg), r in regions.items():
            pool = [r.get("leader"), r.get("top")] + (r.get("newTop") or [])[:3] + \
                   [d.get("leader") for d in r.get("dongs", [])] + r.get("cand", [])
            for x in pool:
                if x:
                    x["_q"] = geo_query(sido, x)
                    recs.append(x)
        n = geocode_all(recs, MAX_GEOCODE)
        print(f"좌표 새로 조회 {n}건")

    write(regions, today, kapt, cli, trade_stop, silv_stop)
    print(f"API 호출 {cli.calls:,}회 · K-apt {kapt.calls:,}회"
          + (f" · K-apt 상태: {kapt.unavailable}" if kapt.unavailable else ""))


def strip(o):
    if isinstance(o, dict):
        return {k: strip(v) for k, v in o.items() if not k.startswith("_")}
    if isinstance(o, list):
        return [strip(v) for v in o]
    return o


def write(regions, today, kapt, cli, trade_stop, silv_stop):
    os.makedirs(OUT, exist_ok=True)
    frm = (today - timedelta(days=365)).isoformat()
    summary, sido_acc = {}, collections.defaultdict(lambda: {"sum": 0, "n": 0, "lead": None})
    by_sido = collections.defaultdict(dict)
    for (sido, sgg), r in regions.items():
        L = r.get("leader")
        summary[f"{sido}|{sgg}"] = {
            "avg": r["stats"]["avg"], "n": r["stats"]["n"], "p84": r["stats"]["p84"],
            "lead": L["name"] if L else None, "leadPpy": L["ppy"] if L else None,
            "relaxed": bool(L and L["tier"] > 0), "cand": len(r.get("cand", [])),
        }
        a = sido_acc[sido]
        a["sum"] += r["stats"]["avg"] * r["stats"]["n"]
        a["n"] += r["stats"]["n"]
        if L and (not a["lead"] or L["ppy"] > a["lead"]["ppy"]):
            a["lead"] = {"name": L["name"], "sgg": sgg, "ppy": L["ppy"], "hh": L.get("hh")}
        by_sido[sido][sgg] = strip(r)
    sido_sum = {s: {"avg": round(a["sum"] / a["n"]) if a["n"] else None, "n": a["n"],
                    "lead": a["lead"]} for s, a in sido_acc.items()}
    meta = {
        "generated": today.isoformat(), "from": frm, "to": today.isoformat(),
        "area": "supply",
        "trades": sum(v["n"] for v in summary.values()),
        "kapt": kapt.unavailable or "ok",
        "partial": scrub(trade_stop) if trade_stop else None,
        "silvPartial": scrub(silv_stop) if silv_stop else None,
        "dongSido": sorted(DONG_SIDO),
    }
    jdump({"meta": meta, "summary": summary, "sidoSummary": sido_sum},
          os.path.join(OUT, "index.json"))
    for sido, payload in by_sido.items():
        jdump(payload, os.path.join(OUT, f"{SLUG[sido]}.json"))
    print(f"web/daejang/index.json {os.path.getsize(os.path.join(OUT, 'index.json')) // 1024} KB, "
          f"시도 파일 {len(by_sido)}개")


if __name__ == "__main__":
    main()
