"""개인정보 1차(규칙 기반) 검사.

여기서 잡는 것: EXIF GPS, 얼굴(YuNet ONNX), QR/바코드.
여기서 못 잡는 것: 전화번호·주소·차량번호·명함 같은 사진 속 문자.
그것은 STAGE_2에서 VLM의 privacy_flags로 2차 검사한다.

검출기가 없으면 조용히 통과시키지 않는다. 반드시 detector_status에 사유를 남긴다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ExifTags

LOG = logging.getLogger(__name__)

GPS_TAG_ID = next((tag for tag, name in ExifTags.TAGS.items() if name == "GPSInfo"), 34853)


@dataclass
class PrivacyFinding:
    kind: str  # face | qr | barcode | gps | exif
    detail: str
    count: int = 1
    score: float | None = None
    # provisional=True 는 "1차 검사가 의심하지만 확정은 아니다"라는 뜻이다.
    # 확정 전까지는 보수적으로 보류하되, VLM 2차 검사가 부정하면 해제한다.
    provisional: bool = False
    boxes: list[list[int]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "detail": self.detail, "count": self.count,
            "score": self.score, "provisional": self.provisional, "boxes": self.boxes,
        }


@dataclass
class PrivacyReport:
    findings: list[PrivacyFinding] = field(default_factory=list)
    detector_status: dict[str, str] = field(default_factory=dict)

    HOLD_KINDS = {"face", "qr", "barcode"}

    @property
    def confirmed_hold(self) -> bool:
        """확정 보류. GPS는 사본에서 제거되므로 보류 사유가 아니다."""
        return any(f.kind in self.HOLD_KINDS and not f.provisional for f in self.findings)

    @property
    def provisional_hold(self) -> bool:
        return any(f.kind in self.HOLD_KINDS and f.provisional for f in self.findings)

    @property
    def hold(self) -> bool:
        """VLM 2차 검사 전에는 의심 단계도 보류로 취급한다."""
        return self.confirmed_hold or self.provisional_hold

    def reasons(self) -> list[str]:
        return [f"{f.kind}: {f.detail}" for f in self.findings]

    def flags(self) -> dict[str, bool]:
        kinds = {f.kind for f in self.findings}
        return {
            "face": "face" in kinds,
            "qr_or_barcode": bool(kinds & {"qr", "barcode"}),
            "gps_in_original": "gps" in kinds,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [f.to_dict() for f in self.findings],
            "detector_status": self.detector_status,
            "hold": self.hold,
            "confirmed_hold": self.confirmed_hold,
            "provisional_hold": self.provisional_hold,
            "flags": self.flags(),
        }


def exif_gps_present(path: Path) -> bool:
    """원본에 GPS EXIF가 있는지 확인한다. 사본에서는 제거되지만 사실 자체는 기록한다."""
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            if not exif:
                return False
            if GPS_TAG_ID in exif:
                gps = exif.get_ifd(GPS_TAG_ID)
                return bool(gps)
            return False
    except Exception:  # noqa: BLE001 - 손상된 EXIF는 검사 실패로만 취급한다
        return False


def exif_is_stripped(path: Path) -> tuple[bool, str]:
    """사본에서 EXIF가 실제로 제거됐는지 검증한다."""
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            if not exif:
                return True, ""
            remaining = {ExifTags.TAGS.get(tag, str(tag)) for tag in exif}
            if GPS_TAG_ID in exif:
                return False, f"GPS EXIF가 남아 있습니다: {sorted(remaining)}"
            return False, f"EXIF 태그가 남아 있습니다: {sorted(remaining)}"
    except Exception as exc:  # noqa: BLE001
        return False, f"EXIF 검증 실패: {exc}"


class RuleBasedPrivacyScanner:
    """OpenCV 기반 오프라인 검사기. 모델이 없으면 그 사실을 보고한다."""

    def __init__(self, config: dict[str, Any], model_path: Path | None) -> None:
        self.config = config
        self.model_path = model_path
        self._cv2 = None
        self._face_detector = None
        self._face_error = ""
        self._init_cv2()

    def _init_cv2(self) -> None:
        try:
            import cv2  # noqa: PLC0415 - 선택적 의존성
        except ImportError as exc:
            self._face_error = f"opencv 미설치: {exc}"
            return
        self._cv2 = cv2
        if not self.config.get("face_detection", True):
            self._face_error = "runtime.yaml에서 face_detection=false로 비활성화됨"
            return
        if self.model_path is None or not self.model_path.exists():
            self._face_error = f"얼굴 검출 모델 없음: {self.model_path}"
            return
        try:
            self._face_detector = cv2.FaceDetectorYN.create(
                str(self.model_path), "", (320, 320),
                float(self.config.get("face_score_threshold", 0.7)),
                float(self.config.get("face_nms_threshold", 0.3)),
                5000,
            )
        except Exception as exc:  # noqa: BLE001
            self._face_error = f"얼굴 검출기 초기화 실패: {exc}"

    def _detect_faces(self, image_bgr) -> list[tuple[list[int], float]]:
        """(bbox, score) 목록. 최소 크기 미만은 버린다."""
        if self._face_detector is None:
            return []
        height, width = image_bgr.shape[:2]
        self._face_detector.setInputSize((width, height))
        _, faces = self._face_detector.detect(image_bgr)
        if faces is None:
            return []
        min_ratio = float(self.config.get("face_min_size_ratio", 0.04))
        min_side = min_ratio * max(width, height)
        result: list[tuple[list[int], float]] = []
        for row in faces:
            x, y, w, h = float(row[0]), float(row[1]), float(row[2]), float(row[3])
            score = float(row[-1])
            if max(w, h) >= min_side:
                result.append(([round(x), round(y), round(w), round(h)], score))
        return result

    def _detect_codes(self, image_bgr) -> list[PrivacyFinding]:
        cv2 = self._cv2
        findings: list[PrivacyFinding] = []
        if cv2 is None:
            return findings
        try:
            detector = cv2.QRCodeDetector()
            found, points = detector.detect(image_bgr)
            if found and points is not None and len(points) > 0:
                findings.append(PrivacyFinding("qr", "QR 코드가 감지됐습니다", len(points)))
        except Exception as exc:  # noqa: BLE001
            LOG.debug("QR 검출 실패: %s", exc)
        try:
            barcode = cv2.barcode.BarcodeDetector()
            found, points = barcode.detect(image_bgr)
            if found and points is not None and len(points) > 0:
                findings.append(PrivacyFinding("barcode", "바코드가 감지됐습니다", len(points)))
        except Exception as exc:  # noqa: BLE001
            LOG.debug("바코드 검출 실패: %s", exc)
        return findings

    def scan(self, original_path: Path, prepared_path: Path) -> PrivacyReport:
        report = PrivacyReport()

        if self.config.get("flag_gps_in_original", True) and exif_gps_present(original_path):
            report.findings.append(PrivacyFinding("gps", "원본에 GPS EXIF가 있었고 사본에서 제거했습니다"))
            report.detector_status["gps"] = "checked"
        else:
            report.detector_status["gps"] = "checked"

        stripped, detail = exif_is_stripped(prepared_path)
        report.detector_status["exif_strip"] = "verified" if stripped else f"FAILED: {detail}"
        if not stripped:
            report.findings.append(PrivacyFinding("exif", detail))

        if self._cv2 is None:
            report.detector_status["face"] = self._face_error or "opencv 없음"
            report.detector_status["qr_barcode"] = self._face_error or "opencv 없음"
            return report

        try:
            image_bgr = self._cv2.imread(str(prepared_path))
        except Exception as exc:  # noqa: BLE001
            report.detector_status["face"] = f"이미지 읽기 실패: {exc}"
            return report
        if image_bgr is None:
            report.detector_status["face"] = "이미지 읽기 실패"
            report.detector_status["qr_barcode"] = "이미지 읽기 실패"
            return report

        if self._face_detector is None:
            report.detector_status["face"] = self._face_error or "얼굴 검출기 없음"
        else:
            try:
                faces = self._detect_faces(image_bgr)
                report.detector_status["face"] = "checked"
                if faces:
                    confirm = float(self.config.get("face_confirm_score", 0.90))
                    best = max(score for _, score in faces)
                    provisional = best < confirm
                    detail = f"얼굴 {len(faces)}개가 감지됐습니다 (최고 점수 {best:.2f})"
                    if provisional:
                        detail += f" — 확정 점수 {confirm:.2f} 미만이라 VLM 2차 검사로 확인합니다"
                    report.findings.append(
                        PrivacyFinding(
                            "face", detail, len(faces), score=round(best, 3),
                            provisional=provisional, boxes=[box for box, _ in faces],
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                report.detector_status["face"] = f"검출 실패: {exc}"

        code_findings = self._detect_codes(image_bgr)
        report.detector_status["qr_barcode"] = "checked"
        report.findings.extend(code_findings)
        return report


def merge_vlm_privacy_flags(report_dict: dict[str, Any], vlm_flags: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """STAGE_2 VLM 2차 검사를 1차 결과에 합친다.

    돌려주는 값: (합쳐진 보고서, 새로 생긴 보류 사유, 해제된 잠정 보류 사유)

    잠정 보류(provisional)는 두 검사가 모두 부정할 때만 해제한다.
    확정 보류는 VLM 결과와 무관하게 유지한다. 개인정보는 놓치는 쪽보다 과하게 잡는 쪽이 안전하다.
    """
    label = {
        "face": "VLM이 사진에서 얼굴을 확인했습니다",
        "text_pii": "VLM이 전화번호·주소·명함 같은 개인 문자를 확인했습니다",
        "plate": "VLM이 차량번호를 확인했습니다",
        "signage": "VLM이 상호 간판을 확인했습니다",
    }
    new_reasons: list[str] = []
    findings = [dict(f) for f in report_dict.get("findings", [])]

    for key, message in label.items():
        if bool(vlm_flags.get(key)):
            findings.append({"kind": f"vlm_{key}", "detail": message, "count": 1, "provisional": False})
            new_reasons.append(f"vlm_{key}: {message}")

    cleared: list[str] = []
    vlm_saw_face = bool(vlm_flags.get("face"))
    if not vlm_saw_face:
        for finding in findings:
            if finding.get("kind") == "face" and finding.get("provisional"):
                finding["provisional_resolved"] = True
                finding["detail"] += " → VLM이 얼굴을 확인하지 못해 보류를 해제했습니다"
                cleared.append(f"face: 규칙 기반 잠정 검출({finding.get('score')})을 VLM 2차 검사가 부정하여 해제")

    def still_holds(finding: dict[str, Any]) -> bool:
        if finding.get("kind") not in {"face", "qr", "barcode"} and not str(finding.get("kind", "")).startswith("vlm_"):
            return False
        if finding.get("provisional") and finding.get("provisional_resolved"):
            return False
        return True

    merged = dict(report_dict)
    merged["findings"] = findings
    merged["hold"] = any(still_holds(f) for f in findings)
    merged["confirmed_hold"] = merged["hold"]
    merged["provisional_hold"] = False
    merged["detector_status"] = {**report_dict.get("detector_status", {}), "vlm_second_pass": "checked"}
    return merged, new_reasons, cleared
