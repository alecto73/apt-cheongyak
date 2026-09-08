# -*- coding: utf-8 -*-
"""시도 이름 -> 파일명용 영문 슬러그.

정적 호스팅(Netlify, S3 등)은 한글 파일명을 업로드할 때
인코딩을 바꿔 저장하는 경우가 있어 브라우저 요청과 어긋난다.
그래서 파일명만 영문으로 쓰고 화면 표시는 한글을 유지한다.
"""
SLUG = {
    "서울": "seoul",
    "부산": "busan",
    "대구": "daegu",
    "인천": "incheon",
    "대전": "daejeon",
    "울산": "ulsan",
    "세종": "sejong",
    "경기": "gyeonggi",
    "강원": "gangwon",
    "충북": "chungbuk",
    "충남": "chungnam",
    "전북": "jeonbuk",
    "전남광주": "jngj",
    "경북": "gyeongbuk",
    "경남": "gyeongnam",
    "제주": "jeju"
}
