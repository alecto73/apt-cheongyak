# -*- coding: utf-8 -*-
"""서버 없이 파일만 열어도 되는 단일 HTML을 만든다.
브라우저는 file:// 에서 fetch 를 막으므로 데이터를 안에 넣는다."""
import json, os
W = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

emb = {
    "index": json.load(open(f"{W}/index.json")),
    "mapNational": json.load(open(f"{W}/map-national.json")),
    "map": {f[:-5]: json.load(open(f"{W}/map/{f}")) for f in os.listdir(f"{W}/map")},
    "region": {f[:-5]: json.load(open(f"{W}/region/{f}"))
               for f in os.listdir(f"{W}/region")},
}

# 읍면동 경계는 build_emd.py 를 돌렸을 때만 있다. 없으면 그 층만 빠지고
# 나머지는 그대로 동작한다.
if os.path.isdir(f"{W}/emd"):
    emb["emd"] = {f[:-5]: json.load(open(f"{W}/emd/{f}"))
                  for f in os.listdir(f"{W}/emd") if f.endswith(".json")}

html = open(f"{W}/index.html", encoding="utf-8").read()
blob = json.dumps(emb, ensure_ascii=False, separators=(",", ":")) \
        .replace("</", "<\\/")
html = html.replace("<script>\nconst E = window.__EMBEDDED__",
                    f"<script>window.__EMBEDDED__={blob};</script>\n<script>\nconst E = window.__EMBEDDED__")
open(f"{W}/standalone.html", "w", encoding="utf-8").write(html)
print("web/standalone.html", os.path.getsize(f"{W}/standalone.html")//1024, "KB")
