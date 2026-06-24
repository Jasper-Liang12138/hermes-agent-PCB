"""Fanout version artifacts integrated with PCB experience memory."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home
from utils import atomic_json_write

from agent.swsd.experience.recorder import PCBExperienceRecorder
from agent.swsd.experience.schema import PCBExperienceEvent


_SCHEMA_VERSION = 1
_ENV_ROOT = "PCB_FANOUT_VERSION_DIR"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_key(value: Any, fallback: str) -> str:
    text = str(value or "").strip() or fallback
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._")
    return cleaned[:120] or fallback


def _sha256_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", errors="replace")).hexdigest()


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(str(text or ""), encoding="utf-8")
    os.replace(tmp_path, path)


def _copy_file(source: str | Path, target: Path) -> bool:
    source_path = Path(str(source or "")).expanduser()
    if not source_path.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target)
    return True


def resolve_fanout_version_root(configured_dir: Optional[str] = None) -> Path:
    raw = os.getenv(_ENV_ROOT, "").strip() or str(configured_dir or "").strip()
    if raw:
        return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    return (get_hermes_home() / "pcb_fanout_versions").resolve()


def _version_from_text(text: str) -> Optional[int]:
    source = str(text or "")
    match = re.search(r"(?:第\s*)?(\d+)\s*(?:版|轮|次|回合)", source, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"\bv\s*0*(\d+)\b", source, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def parse_fanout_version_request(text: Any) -> dict[str, Any]:
    """Parse user intent to list, restore, or rerun fanout versions."""

    source = str(text or "").strip()
    if not source:
        return {"isVersionRequest": False}
    auto_route = bool(
        re.search(r"然后布线|直接(?:开始|执行|布线)|确认(?:执行|布线)?|马上|立刻|route\b|run\b|go\b", source, re.IGNORECASE)
    ) and not bool(re.search(r"不要|别|先别|不用|无需|只生成|仅生成|只恢复", source))
    if re.search(r"有哪些.*(?:版本|历史)|列出.*(?:版本|历史)|fanout.*历史|布线.*历史", source, re.IGNORECASE):
        return {"isVersionRequest": True, "intent": "list_versions", "autoRoute": False}

    has_iteration = bool(re.search(r"再来一轮|再跑一轮|重新跑一轮|再试一轮|迭代|下一轮|重新生成", source, re.IGNORECASE))
    has_restore = bool(re.search(r"回到|恢复|还原|切回|退回|选用|采用|用|按|基于", source, re.IGNORECASE))
    mentions_param = bool(re.search(r"参数|层分配|逃逸顺序|fanout\s*params|order\s*lines?", source, re.IGNORECASE))
    mentions_layout = bool(re.search(r"版图|板图|layout|结果|routed", source, re.IGNORECASE))
    mentions_version = _version_from_text(source) is not None or bool(re.search(r"初始|原始|当前|最新|上一轮|最后", source))
    if not (has_iteration or (has_restore and mentions_version) or (mentions_version and (mentions_param or mentions_layout))):
        return {"isVersionRequest": False}

    version = _version_from_text(source)
    restored_from: int | str | None = None
    base_layout: int | str | None = None
    if re.search(r"初始|原始", source):
        base_layout = 0 if mentions_layout or re.search(r"从.*(?:初始|原始)", source) else None
        restored_from = restored_from if base_layout == 0 else 0
    if re.search(r"当前|最新|上一轮|最后", source):
        if mentions_layout or re.search(r"从.*(?:当前|最新|上一轮|最后)", source):
            base_layout = "current"
        elif mentions_param:
            restored_from = "current"
    if version is not None:
        if mentions_layout:
            base_layout = version
        if mentions_param or restored_from is None:
            restored_from = version
    if has_restore and mentions_param:
        intent = "rerun_from_params" if has_iteration or auto_route else "restore_params"
    elif has_iteration and restored_from is not None and mentions_param:
        intent = "rerun_from_params"
    elif has_iteration or base_layout is not None:
        intent = "iterate_from_layout"
    else:
        intent = "restore_params"
    return {
        "isVersionRequest": True,
        "intent": intent,
        "baseLayoutVersion": base_layout,
        "restoredFromVersion": restored_from,
        "autoRoute": auto_route,
        "userOverrideText": source,
    }


class FanoutVersionStore:
    """Disk-backed fanout version store plus compact experience events."""

    def __init__(self, db: Any = None, configured_dir: Optional[str] = None) -> None:
        self.db = db
        self._configured_dir = configured_dir
        self._lock = threading.RLock()
        self._session_dirs: dict[str, Path] = {}
        self._session_projects: dict[str, str] = {}

    @property
    def root(self) -> Path:
        return resolve_fanout_version_root(self._configured_dir)

    def bind_project(self, session_id: str, project_id: str) -> None:
        if session_id and project_id:
            self._session_projects[str(session_id)] = str(project_id)

    def _history_path(self, session_id: str) -> Optional[Path]:
        path = self._session_dirs.get(str(session_id or ""))
        return path / "history.json" if path else None

    def _load_history_path(self, session_id: str) -> tuple[Optional[Path], dict[str, Any]]:
        path = self._history_path(session_id)
        if not path:
            return None, {}
        history = _read_json(path, {})
        return path, history if isinstance(history, dict) else {}

    def _write_history(self, path: Path, history: dict[str, Any]) -> None:
        history["updatedAt"] = _utc_now()
        atomic_json_write(path, history)

    def _record_event(
        self,
        *,
        session_id: str,
        project_id: str,
        kind: str,
        stage: str,
        outcome: str,
        summary: str,
        signals: dict[str, Any],
    ) -> None:
        PCBExperienceRecorder(self.db).record(
            PCBExperienceEvent(
                kind=kind,
                session_id=session_id,
                project_id=project_id,
                workflow_id="pcb_escape_flow",
                stage=stage,
                outcome=outcome,
                summary=summary,
                signals=signals,
                source="fanout_version_store",
                confidence=0.9,
            )
        )

    def record_initial_layout(self, session_id: str, project_id: str = "", layout_text: str = "") -> dict[str, Any]:
        layout_text = str(layout_text or "")
        if not session_id or not layout_text:
            return {}
        fingerprint = _sha256_text(layout_text)
        project = str(project_id or "").strip() or self._session_projects.get(session_id, "")
        project_key = f"{_safe_key(project, 'unknown_project')}_{fingerprint[:12]}"
        session_key = _safe_key(session_id, "session")
        base_dir = self.root / project_key / session_key
        history_path = base_dir / "history.json"
        with self._lock:
            self._session_dirs[str(session_id)] = base_dir
            base_dir.mkdir(parents=True, exist_ok=True)
            version_dir = base_dir / "v000"
            layout_path = version_dir / "layout.txt"
            if not layout_path.is_file():
                _write_text(layout_path, layout_text)
            history = _read_json(history_path, {})
            if not isinstance(history, dict) or history.get("schemaVersion") != _SCHEMA_VERSION:
                now = _utc_now()
                history = {
                    "schemaVersion": _SCHEMA_VERSION,
                    "projectId": project,
                    "sessionId": session_id,
                    "boardFingerprint": fingerprint,
                    "createdAt": now,
                    "updatedAt": now,
                    "latestVersion": 0,
                    "currentLayoutVersion": 0,
                    "activeParamsVersion": None,
                    "versions": [
                        {
                            "version": 0,
                            "kind": "initial_layout",
                            "status": "available",
                            "createdAt": now,
                            "files": {"layout": "v000/layout.txt"},
                        }
                    ],
                }
            self._write_history(history_path, history)
        self._record_event(
            session_id=session_id,
            project_id=project,
            kind="fanout_version",
            stage="select_bga",
            outcome="initial_layout",
            summary="Recorded initial PCB layout for fanout version memory.",
            signals=self.history_summary(session_id),
        )
        return history

    def load_history(self, session_id: str) -> dict[str, Any]:
        _path, history = self._load_history_path(session_id)
        return history

    def has_history(self, session_id: str) -> bool:
        versions = self.load_history(session_id).get("versions")
        return isinstance(versions, list) and bool(versions)

    def _version_record(self, history: dict[str, Any], version: int) -> Optional[dict[str, Any]]:
        for record in history.get("versions") or []:
            if isinstance(record, dict) and int(record.get("version", -1)) == int(version):
                return record
        return None

    def _coerce_version(self, history: dict[str, Any], value: Any, *, for_layout: bool = False) -> Optional[int]:
        if value in (None, ""):
            return None
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"current", "latest", "last"}:
                key = "currentLayoutVersion" if for_layout else "latestVersion"
                try:
                    return int(history.get(key, history.get("latestVersion", 0)))
                except (TypeError, ValueError):
                    return None
        try:
            version = int(value)
        except (TypeError, ValueError):
            return None
        return version if self._version_record(history, version) is not None else None

    def resolve_layout_text(self, session_id: str, version: Any = None) -> tuple[str, Optional[int], str]:
        history_path, history = self._load_history_path(session_id)
        if not history_path:
            return "", None, ""
        version_int = self._coerce_version(history, version, for_layout=True)
        if version_int is None:
            version_int = self._coerce_version(history, "current", for_layout=True)
        if version_int is None:
            return "", None, ""
        record = self._version_record(history, version_int)
        files = record.get("files") if isinstance(record, dict) and isinstance(record.get("files"), dict) else {}
        rel_path = files.get("layout") if version_int == 0 else files.get("routedLayout") or files.get("layout")
        if not rel_path:
            return "", version_int, ""
        path = (history_path.parent / str(rel_path)).resolve()
        if not path.is_file():
            return "", version_int, str(path)
        return path.read_text(encoding="utf-8", errors="replace"), version_int, str(path)

    def fanout_params_for_version(self, session_id: str, version: Any) -> tuple[dict[str, Any], Optional[int]]:
        history_path, history = self._load_history_path(session_id)
        if not history_path:
            return {}, None
        version_int = self._coerce_version(history, version, for_layout=False)
        if version_int is None or version_int == 0:
            return {}, version_int
        record = self._version_record(history, version_int)
        files = record.get("files") if isinstance(record, dict) and isinstance(record.get("files"), dict) else {}
        rel_path = files.get("fanoutParams")
        if not rel_path:
            return {}, version_int
        data = _read_json(history_path.parent / str(rel_path), {})
        return data if isinstance(data, dict) else {}, version_int

    def latest_fanout_params(self, session_id: str) -> tuple[dict[str, Any], Optional[int]]:
        history = self.load_history(session_id)
        return self.fanout_params_for_version(session_id, history.get("latestVersion"))

    def write_draft(
        self,
        session_id: str,
        *,
        fanout_params: dict[str, Any],
        user_text: str = "",
        base_layout_version: Any = None,
        restored_from_version: Any = None,
    ) -> dict[str, Any]:
        history_path, history = self._load_history_path(session_id)
        if not history_path or not isinstance(fanout_params, dict) or not fanout_params:
            return {}
        base_version = self._coerce_version(history, base_layout_version, for_layout=True)
        if base_version is None:
            base_version = self._coerce_version(history, "current", for_layout=True)
        restored_version = self._coerce_version(history, restored_from_version, for_layout=False)
        draft = {
            "draftId": f"draft_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}",
            "status": "params_ready",
            "createdAt": _utc_now(),
            "baseLayoutVersion": base_version,
            "restoredFromVersion": restored_version,
            "userText": str(user_text or "")[:4000],
            "fanoutParams": fanout_params,
        }
        with self._lock:
            atomic_json_write(history_path.parent / "draft.json", draft)
            history["activeParamsVersion"] = restored_version
            self._write_history(history_path, history)
        return draft

    def load_draft(self, session_id: str) -> dict[str, Any]:
        history_path = self._history_path(session_id)
        if not history_path:
            return {}
        draft = _read_json(history_path.parent / "draft.json", {})
        return draft if isinstance(draft, dict) else {}

    def record_route_version(
        self,
        session_id: str,
        *,
        fanout_params: dict[str, Any],
        project_id: str = "",
        base_layout_version: Any = None,
        restored_from_version: Any = None,
        user_text: str = "",
        order_input_path: str = "",
        routed_layout_path: str = "",
        import_lines_path: str = "",
        report: str = "",
    ) -> dict[str, Any]:
        history_path, history = self._load_history_path(session_id)
        if not history_path or not isinstance(fanout_params, dict) or not fanout_params:
            return {}
        latest = int(history.get("latestVersion") or 0)
        version = latest + 1
        version_name = f"v{version:03d}"
        version_dir = history_path.parent / version_name
        version_dir.mkdir(parents=True, exist_ok=True)
        base_version = self._coerce_version(history, base_layout_version, for_layout=True)
        if base_version is None:
            base_version = self._coerce_version(history, "current", for_layout=True)
        restored_version = self._coerce_version(history, restored_from_version, for_layout=False)

        fanout_path = version_dir / "fanout_params.json"
        atomic_json_write(fanout_path, fanout_params)
        files = {"fanoutParams": f"{version_name}/fanout_params.json"}
        if _copy_file(order_input_path, version_dir / "order_input.txt"):
            files["orderInput"] = f"{version_name}/order_input.txt"
        if _copy_file(routed_layout_path, version_dir / "routed_layout.txt"):
            files["routedLayout"] = f"{version_name}/routed_layout.txt"
        if _copy_file(import_lines_path, version_dir / "import_lines.out"):
            files["importLines"] = f"{version_name}/import_lines.out"
        _write_text(version_dir / "report.txt", str(report or ""))
        files["report"] = f"{version_name}/report.txt"

        order_lines = fanout_params.get("orderLines") if isinstance(fanout_params.get("orderLines"), list) else []
        nl_order_lines = fanout_params.get("naturalLanguageOrderLines")
        record = {
            "version": version,
            "kind": "fanout_run",
            "status": "routed",
            "parentVersion": latest,
            "baseLayoutVersion": base_version,
            "restoredFromVersion": restored_version,
            "createdAt": _utc_now(),
            "updatedAt": _utc_now(),
            "userText": str(user_text or "")[:4000],
            "selectedBGA": str(fanout_params.get("selectedBGA") or ""),
            "routerType": str(fanout_params.get("routerType") or ""),
            "orderLineCount": len(order_lines),
            "naturalLanguageOrderLineCount": len(nl_order_lines) if isinstance(nl_order_lines, list) else 0,
            "importStatus": "pending",
            "files": files,
        }
        with self._lock:
            history.setdefault("versions", []).append(record)
            history["latestVersion"] = version
            self._write_history(history_path, history)
        summary = self.history_summary(session_id)
        self._record_event(
            session_id=session_id,
            project_id=project_id or str(history.get("projectId") or ""),
            kind="fanout_version",
            stage="routing",
            outcome="route_version",
            summary=f"Recorded fanout route version v{version:03d}.",
            signals=summary,
        )
        return {"version": version, "record": record, "history": history}

    def mark_import_status(self, session_id: str, version: Any, status: str, message: str = "") -> dict[str, Any]:
        history_path, history = self._load_history_path(session_id)
        if not history_path:
            return {}
        version_int = self._coerce_version(history, version, for_layout=False)
        if version_int is None or version_int == 0:
            return history
        record = self._version_record(history, version_int)
        if not record:
            return history
        normalized = str(status or "").strip().lower() or "unknown"
        with self._lock:
            record["importStatus"] = normalized
            record["importMessage"] = str(message or "")[:1000]
            record["updatedAt"] = _utc_now()
            if normalized == "success":
                history["currentLayoutVersion"] = version_int
            self._write_history(history_path, history)
        return history

    def history_summary(self, session_id: str, limit: int = 12) -> dict[str, Any]:
        history = self.load_history(session_id)
        if not history:
            return {}
        versions = [item for item in history.get("versions") or [] if isinstance(item, dict)]
        compact = []
        for item in versions[-limit:]:
            compact.append(
                {
                    "version": item.get("version"),
                    "kind": item.get("kind"),
                    "status": item.get("status"),
                    "selectedBGA": item.get("selectedBGA"),
                    "routerType": item.get("routerType"),
                    "baseLayoutVersion": item.get("baseLayoutVersion"),
                    "restoredFromVersion": item.get("restoredFromVersion"),
                    "orderLineCount": item.get("orderLineCount"),
                    "naturalLanguageOrderLineCount": item.get("naturalLanguageOrderLineCount"),
                    "importStatus": item.get("importStatus"),
                    "createdAt": item.get("createdAt"),
                }
            )
        return {
            "latestVersion": history.get("latestVersion"),
            "currentLayoutVersion": history.get("currentLayoutVersion"),
            "activeParamsVersion": history.get("activeParamsVersion"),
            "versions": compact,
        }
