# -*- coding: utf-8 -*-
"""서버 없이 파일만 열어도 되는 단일 HTML을 만든다.
브라우저는 file:// 에서 fetch 를 막으므로 데이터를 안에 넣는다."""
import json, os
W = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# 자료는 전부 한글이 들어 있다. encoding 을 적지 않으면 파이썬이
# 운영체제 기본 인코딩을 쓰는데, 한국어 윈도우에서는 그게 cp949 라
# UnicodeDecodeError 가 난다. 읽고 쓰는 모든 곳에 utf-8 을 못박는다.
def load(path):
    return json.load(open(path, encoding="utf-8"))


emb = {
    "index": load(f"{W}/index.json"),
    "mapNational": load(f"{W}/map-national.json"),
    "map": {f[:-5]: load(f"{W}/map/{f}") for f in os.listdir(f"{W}/map")},
    "region": {f[:-5]: load(f"{W}/region/{f}")
               for f in os.listdir(f"{W}/region")},
}

# 읍면동 경계는 build_emd.py 를 돌렸을 때만 있다. 없으면 그 층만 빠지고
# 나머지는 그대로 동작한다.
if os.path.isdir(f"{W}/emd"):
    emb["emd"] = {f[:-5]: load(f"{W}/emd/{f}")
                  for f in os.listdir(f"{W}/emd") if f.endswith(".json")}

html = open(f"{W}/index.html", encoding="utf-8").read()
blob = json.dumps(emb, ensure_ascii=False, separators=(",", ":")) \
        .replace("</", "<\\/")
html = html.replace("<script>\nconst E = window.__EMBEDDED__",
                    f"<script>window.__EMBEDDED__={blob};</script>\n<script>\nconst E = window.__EMBEDDED__")
open(f"{W}/standalone.html", "w", encoding="utf-8").write(html)
print("web/standalone.html", os.path.getsize(f"{W}/standalone.html")//1024, "KB")
