# -*- coding: utf-8 -*-
"""공고 주소를 좌표로 바꿔 web/geo-cache.json 에 쌓는다.

청약홈 API는 좌표를 주지 않고 주소와 우편번호만 준다. 우편번호는
100% 채워져 있고 고유값이 800개대라, 한 번 좌표를 구해 캐시해두면
이후 갱신에서는 새로 나온 우편번호만 조회하면 된다.

OpenStreetMap Nominatim 을 쓴다. 인증키가 필요 없는 대신 초당 1회
제한이 있어 느리지만, 캐시가 차면 실제 조회는 거의 일어나지 않는다.
카카오나 브이월드 지오코더로 바꾸려면 lookup() 만 교체하면 된다.

  python geocode.py            # 캐시에 없는 것만 조회
  python geocode.py --retry    # 실패로 기록된 것도 다시 시도
"""
import json, os, re, sys, time, urllib.parse, urllib.request
from datetime import date

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "web", "geo-cache.json")
UA = {"User-Agent": "apt-cheongyak/1.0 (public housing data viewer)"}
DELAY = 1.2          # Nominatim 이용 정책: 초당 1회 이하
YEARS_BACK = 3


def lookup(**params):
    params.update(format="json", limit=1, countrycodes="kr")
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=UA), timeout=30) as r:
                d = json.load(r)
            return [round(float(d[0]["lat"]), 5), round(float(d[0]["lon"]), 5)] if d else None
        except Exception:
            time.sleep(3 * (attempt + 1))
    return None


def clean_addr(a):
    """'충청남도 천안시 서북구 부대동 384-20번지 일원' -> 동까지만 남긴다."""
    a = re.sub(r"\(.*?\)", " ", a or "")
    a = re.sub(r"(번지|일원|일대|블록|블럭|지구|외\s*\d+\S*|산\s*\d+\S*)", " ", a)
    a = re.sub(r"\d+(-\d+)?", " ", a)
    a = a.replace("특례시", "시")
    toks = [t for t in a.split() if t]
    return " ".join(toks[:4])


def main():
    retry = "--retry" in sys.argv
    cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}

    notices = json.load(open(os.path.join(ROOT, "raw", "notice.json"), encoding="utf-8"))
    frm = date.today().replace(year=date.today().year - YEARS_BACK).isoformat()
    rows = [n for n in notices if (n.get("RCRIT_PBLANC_DE") or "") >= frm]

    todo = {}
    for n in rows:
        z = str(n.get("HSSPLY_ZIP") or "").strip()
        key = z if z.isdigit() else clean_addr(n.get("HSSPLY_ADRES"))
        if not key:
            continue
        if key in cache and not (retry and cache[key] is None):
            continue
        todo[key] = n.get("HSSPLY_ADRES") or ""

    print(f"공고 {len(rows)}건 / 조회 대상 {len(todo)}개 (캐시 {len(cache)}개)")
    done = 0
    for key, addr in todo.items():
        hit = lookup(postalcode=key) if key.isdigit() else None
        if hit is None:
            time.sleep(DELAY)
            hit = lookup(q=clean_addr(addr))
        cache[key] = hit
        done += 1
        if done % 25 == 0 or done == len(todo):
            json.dump(cache, open(CACHE, "w", encoding="utf-8"),
                      ensure_ascii=False, separators=(",", ":"))
            got = sum(1 for v in cache.values() if v)
            print(f"  {done}/{len(todo)} 완료 · 캐시 적중 {got}/{len(cache)}", flush=True)
        time.sleep(DELAY)

    json.dump(cache, open(CACHE, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    got = sum(1 for v in cache.values() if v)
    print(f"끝. 좌표 확보 {got}/{len(cache)} ({got / max(len(cache),1) * 100:.1f}%)")


if __name__ == "__main__":
    main()
