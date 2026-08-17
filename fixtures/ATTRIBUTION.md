# 테스트 fixture 이미지 출처

이 폴더의 이미지는 **파이프라인 검증용 대체 입력**이다. 운영에서는 사용자가 직접 촬영한 사진이 `inbox/`로 들어오며,
그 경우 라이선스 문제는 발생하지 않는다. 여기 있는 이미지는 장비에 사용자 사진이 없어 실행 검증용으로 받아온 것이다.

## `kitchen/blue-white-kitchen.jpg`

- Title: Blue white kitchen interior (Unsplash)
- License: **CC0** (퍼블릭 도메인 기증)
- Source: https://commons.wikimedia.org/wiki/File:Blue_white_kitchen_interior_(Unsplash).jpg
- Local derivative: Wikimedia 썸네일 API로 긴 변 1600px 축소본을 내려받음

## `kitchen/terrytown-kitchen.jpg`

- Title: Terrytown Louisiana Kitchen Interior
- License: **CC BY-SA 4.0**
- Source: https://commons.wikimedia.org/wiki/File:Terrytown_Louisiana_Kitchen_Interior.jpg
- Local derivative: 긴 변 1600px 축소본

## `smoke/kitchen-cabinets-sink.jpg`

기존 fixture. `smoke/ATTRIBUTION.md` 참조 (amslerPIX, CC BY 2.0).

## `privacy/portrait-face.jpg`

- Title: Emma - Natural Light
- Creator: Lies Thru a Lens (Flickr)
- License: **CC BY 2.0**
- Source: https://commons.wikimedia.org/wiki/File:Emma_-_Natural_Light.jpg
- 용도: **개인정보 검사 회귀 테스트 전용.** 얼굴이 검출되어 `PRIVACY_HOLD`로 분류되는지 확인한다.
  이 이미지는 블로그 본문에 실리지 않는 것이 테스트의 통과 조건이다.

내려받은 원본 메타데이터는 `kitchen/_download_meta.json`에 sha256과 함께 기록했다.
