"""FastAPI server, persistent chat API, and SSE workflow streaming."""

from __future__ import annotations

import json
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .artifact_origin import origin_for
from .chat_models import ArtifactRecord, ChatMessage, ChatSettings
from .chat_store import ChatStore
from .config import current_settings, load_settings, override_current_settings
from .domains import available_packs, load_pack
from .errors import OUT_OF_MONEY_MESSAGE, is_out_of_money_error
from .logging_setup import setup_logging, workflow_event_sink_var
from .memory import summarize_title_with_fast_model, summarize_with_fast_model
from .projects import ProjectStore, active_project
from .skills import list_skills
from .usage import usage_run_context
from .workflow import WorkflowExecutor, write_followup_docx, write_followup_pdf


_settings = load_settings()
setup_logging(_settings)

app = FastAPI(title="Legal AI Helper", version="0.3.0")


@app.get("/domains")
async def get_domains() -> dict:
    """List installed domain packs with metadata."""
    out = []
    for name in available_packs():
        try:
            pack = load_pack(name)
            out.append(
                {
                    "name": pack.name,
                    "display_name": pack.display_name,
                    "description": pack.description,
                    "jurisdictions": list(pack.jurisdictions),
                    "mcp_servers": list(pack.mcp_servers),
                    "skill_overlays": list(pack.skill_overlays.keys()),
                }
            )
        except Exception as e:  # noqa: BLE001
            out.append({"name": name, "error": str(e)})
    return {"packs": out, "active": list(current_settings().active_domain_packs)}


@app.get("/jurisdictions")
async def get_jurisdictions() -> dict:
    s = current_settings()
    return {
        "default": s.default_jurisdiction,
        "secondary": list(s.secondary_jurisdictions),
        "citation_style": s.citation_style,
        "default_language": s.default_language,
        "supported": ["CN", "US", "EU", "UK", "HK", "ET", "ICAO"],
    }

_PKG_DIR = Path(__file__).parent
_WEBUI_DIR = _PKG_DIR / "webui"
_WEBUI_DIST = _PKG_DIR / "webui_dist"

if (_WEBUI_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(_WEBUI_DIST / "assets")), name="assets")
app.mount("/static", StaticFiles(directory=str(_WEBUI_DIR)), name="static")

_RUN_SUBSCRIBERS: dict[str, set[queue.Queue[Optional[tuple[str, Any]]]]] = {}
_RUN_CANCEL_EVENTS: dict[str, threading.Event] = {}
_RUN_LOCK = threading.Lock()


class ChatRequest(BaseModel):
    message: str
    attachments: list[str] = []
    skill_hint: Optional[str] = None
    chat_id: Optional[str] = None
    settings: Optional[ChatSettings] = None


class ChatCreateRequest(BaseModel):
    title: Optional[str] = None
    settings: Optional[ChatSettings] = None


class ChatPatchRequest(BaseModel):
    title: Optional[str] = None
    archived: Optional[bool] = None
    settings: Optional[ChatSettings] = None


class ChatStreamRequest(BaseModel):
    message: str
    attachments: list[str] = []
    skill_hint: Optional[str] = None
    settings: Optional[ChatSettings] = None


class ProjectCreateRequest(BaseModel):
    name: str
    slug: Optional[str] = None
    brief: str = ""
    jurisdiction: Optional[str] = None
    domain_packs: list[str] = []


class ProjectPatchRequest(BaseModel):
    name: Optional[str] = None
    brief: Optional[str] = None
    summary: Optional[str] = None
    jurisdiction: Optional[str] = None
    domain_packs: Optional[list[str]] = None
    status: Optional[str] = None


class ProjectMemoryRequest(BaseModel):
    kind: str
    title: str
    body: str = ""
    salience: int = 3
    status: str = "open"


class ChatProjectAssignRequest(BaseModel):
    project_id: Optional[str] = None


class ExportRequest(BaseModel):
    title: Optional[str] = None
    filename: Optional[str] = None
    format: Optional[str] = None
    # When the citation audit returned a DO-NOT-FILE verdict, the UI passes the
    # watermark text so the exported PDF/DOCX is stamped and cannot be mistaken
    # for a filed document. Empty / None → no watermark.
    watermark: Optional[str] = None


def _store() -> ChatStore:
    return ChatStore(current_settings())


try:
    _store().mark_orphaned_runs()
except Exception:
    pass


def _chat_outputs_dir(chat_id: str) -> Path:
    safe_chat_id = _safe_chat_id(chat_id)
    target = current_settings().outputs_dir / "chat_artifacts" / safe_chat_id
    target.mkdir(parents=True, exist_ok=True)
    return target


def _chat_uploads_dir(chat_id: str) -> Path:
    safe_chat_id = _safe_chat_id(chat_id)
    target = current_settings().outputs_dir / "chat_uploads" / safe_chat_id
    target.mkdir(parents=True, exist_ok=True)
    return target


def _safe_chat_id(chat_id: str) -> str:
    return "".join(ch for ch in chat_id if ch.isalnum() or ch in {"_", "-"})


def _safe_upload_filename(filename: str) -> str:
    name = Path(filename).name
    return "".join(ch if ch.isalnum() or ch in {" ", ".", "_", "-"} else "_" for ch in name).strip() or "upload"


def _artifact_url(chat_id: str, artifact_id: str) -> str:
    return f"/api/chats/{chat_id}/artifacts/{artifact_id}"


def _artifact_payload(artifact: ArtifactRecord) -> dict[str, Any]:
    payload = artifact.model_dump()
    payload["url"] = _artifact_url(artifact.chat_id, artifact.id)
    return payload


def _best_export_source_message(store: ChatStore, chat_id: str, current_assistant_id: Optional[str] = None) -> Optional[ChatMessage]:
    messages = [
        m
        for m in store.list_messages(chat_id)
        if m.role == "assistant" and m.id != current_assistant_id and m.content.strip()
    ]
    if not messages:
        return None

    def is_export_ack(text: str) -> bool:
        lower = text.lower().strip()
        return (
            lower.startswith("已生成 pdf")
            or lower.startswith("已生成 docx")
            or lower.startswith("generated pdf")
            or lower.startswith("generated docx")
            or "(/api/chats/" in lower and ("pdf" in lower or "docx" in lower)
        )

    def is_clarification_or_router_reply(text: str) -> bool:
        lower = " ".join(text.lower().split())
        return (
            "what findings should i include" in lower
            or "please paste or summarize" in lower
            or "i can route it" in lower
            or "please share the message" in lower
            or lower.startswith("which agreement or clause")
        )

    substantive = [
        m
        for m in messages
        if len(m.content.strip()) >= 300 and not is_export_ack(m.content) and not is_clarification_or_router_reply(m.content)
    ]
    if substantive:
        return substantive[-1]
    usable = [m for m in messages if not is_export_ack(m.content) and not is_clarification_or_router_reply(m.content)]
    return usable[-1] if usable else messages[-1]


def _write_followup_artifact(
    settings,
    source_text: str,
    title: str,
    artifact_format: str,
    *,
    watermark: Optional[str] = None,
) -> dict[str, str]:
    if artifact_format == "docx":
        return write_followup_docx(settings, source_text, title, watermark=watermark)
    return write_followup_pdf(settings, source_text, title, watermark=watermark)


def _is_chat_artifact_path(path: str | Path, chat_id: str) -> bool:
    p = Path(path).resolve()
    try:
        p.relative_to((load_settings().outputs_dir / "chat_artifacts" / _safe_chat_id(chat_id)).resolve())
    except ValueError:
        return False
    return p.is_file()


def _is_chat_upload_path(path: str | Path, chat_id: str) -> bool:
    p = Path(path).resolve()
    try:
        p.relative_to((load_settings().outputs_dir / "chat_uploads" / _safe_chat_id(chat_id)).resolve())
    except ValueError:
        return False
    return p.is_file()


def _validated_attachment_path(chat_id: str, path: str) -> Path:
    p = Path(path).resolve()
    if not _is_chat_upload_path(p, chat_id):
        raise HTTPException(status_code=400, detail="Attachment does not belong to this chat")
    return p


def _chat_context_attachment_paths(
    store: ChatStore,
    chat_id: str,
    current_paths: list[Path] | None = None,
) -> list[Path]:
    """Return current + previously uploaded files that belong to this chat.

    Uploads are chat-scoped on disk, while sent attachments are also recorded
    in message metadata. Including both sources makes follow-ups like
    "use the Word document I uploaded earlier" work without re-uploading.
    """
    seen: set[str] = set()
    out: list[Path] = []

    def add(path: str | Path) -> None:
        p = Path(path).resolve()
        key = str(p)
        if key in seen or not _is_chat_upload_path(p, chat_id):
            return
        seen.add(key)
        out.append(p)

    for p in current_paths or []:
        add(p)
    for message in store.list_messages(chat_id):
        for raw in message.metadata.get("attachments") or []:
            add(raw)

    upload_root = load_settings().outputs_dir / "chat_uploads" / _safe_chat_id(chat_id)
    if upload_root.is_dir():
        files = [p for p in upload_root.glob("*/*") if p.is_file()]
        for p in sorted(files, key=lambda item: item.stat().st_mtime):
            add(p)
    return out


def _list_visible_artifacts(store: ChatStore, chat_id: str) -> list[ArtifactRecord]:
    return [a for a in store.list_artifacts(chat_id) if _is_chat_artifact_path(a.path, chat_id)]


def _maybe_generate_chat_title(
    store: ChatStore,
    chat_id: str,
    settings,
    emit: Optional[Callable[[str, Any], None]] = None,
) -> None:
    chat = store.get_chat(chat_id)
    if chat.title != "New chat":
        return
    title = summarize_title_with_fast_model(settings, store.list_messages(chat_id, limit=8))
    if title == "New chat":
        return
    updated = store.update_chat(chat_id, title=title)
    if emit is not None:
        emit("chat_updated", updated.model_dump())


def _settings_for_chat(chat_settings: Optional[ChatSettings], *, chat_id: Optional[str] = None):
    base = current_settings()
    updates: dict[str, Any] = {}
    if chat_settings is None:
        pass
    else:
        updates.update(
            {
                "provider": chat_settings.provider,
                "enable_web_search": chat_settings.enable_web_search,
                "enable_web_fetch": chat_settings.enable_web_fetch,
            }
        )
        if chat_settings.provider == "openai":
            updates["openai_model"] = chat_settings.model
            updates["openai_reasoning_effort"] = chat_settings.reasoning_effort
        else:
            updates["anthropic_model"] = chat_settings.model
    if chat_id:
        updates["outputs_dir"] = _chat_outputs_dir(chat_id)
        # A project pins jurisdiction + packs for every chat under it, so the
        # right connectors (e.g. ET/aviation for the Addis route) surface and
        # the orchestrator inherits the project's standing context.
        project = _project_for_chat(chat_id)
        if project is not None:
            if project.jurisdiction:
                updates.setdefault("default_jurisdiction", project.jurisdiction)
            if project.domain_packs:
                updates.setdefault("active_domain_packs", list(project.domain_packs))
    return base.model_copy(update=updates)


def _project_store() -> ProjectStore:
    return ProjectStore(current_settings())


def _project_for_chat(chat_id: str):
    try:
        store = _project_store()
        pid = store.project_id_for_chat(chat_id)
        return store.get_project(pid) if pid else None
    except Exception:  # noqa: BLE001 — project context is best-effort
        return None


def _dedupe(items: list[str]) -> list[str]:
    """Order-preserving de-duplication (fast model may equal a listed model)."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _sse(event: str, data: Any) -> bytes:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


def _subscribe_run(run_id: str) -> queue.Queue[Optional[tuple[str, Any]]]:
    q: queue.Queue[Optional[tuple[str, Any]]] = queue.Queue()
    with _RUN_LOCK:
        _RUN_SUBSCRIBERS.setdefault(run_id, set()).add(q)
    return q


def _register_run_cancel_event(run_id: str) -> threading.Event:
    event = threading.Event()
    with _RUN_LOCK:
        _RUN_CANCEL_EVENTS[run_id] = event
    return event


def _cancel_run_event(run_id: str) -> bool:
    with _RUN_LOCK:
        event = _RUN_CANCEL_EVENTS.get(run_id)
    if event is None:
        return False
    event.set()
    return True


def _unregister_run_cancel_event(run_id: str) -> None:
    with _RUN_LOCK:
        _RUN_CANCEL_EVENTS.pop(run_id, None)


def _unsubscribe_run(run_id: str, q: queue.Queue[Optional[tuple[str, Any]]]) -> None:
    with _RUN_LOCK:
        subscribers = _RUN_SUBSCRIBERS.get(run_id)
        if subscribers is None:
            return
        subscribers.discard(q)
        if not subscribers:
            _RUN_SUBSCRIBERS.pop(run_id, None)


def _publish_run(run_id: str, event: str, data: Any) -> None:
    with _RUN_LOCK:
        subscribers = list(_RUN_SUBSCRIBERS.get(run_id, set()))
    for q in subscribers:
        q.put((event, data))


def _finish_run_streams(run_id: str) -> None:
    with _RUN_LOCK:
        subscribers = list(_RUN_SUBSCRIBERS.pop(run_id, set()))
    for q in subscribers:
        q.put(None)


def _mark_run_cancelled(store: ChatStore, run_id: str, message: str = "Cancelled by user.") -> dict[str, Any]:
    run = store.get_run(run_id)
    assistant_msg = store.get_message(run.assistant_message_id)
    metadata = {**assistant_msg.metadata, "run_status": "cancelled"}
    store.update_message(
        assistant_msg.id,
        assistant_msg.content or "Response cancelled.",
        metadata,
    )
    updated = store.update_run(run.id, status="cancelled", error=message, finished=True)
    event = store.add_event(
        run.chat_id,
        "run_cancelled",
        {"run_id": run.id, "message": message},
    )
    payload = {
        "run": updated.model_dump(),
        "message": message,
        "event": event.model_dump(),
    }
    _publish_run(run.id, "cancelled", payload)
    _finish_run_streams(run.id)
    return payload


def _scan_new_artifacts(
    store: ChatStore,
    chat_id: str,
    seen_paths: set[str],
    artifact_dir: Path,
    message_id: Optional[str] = None,
):
    for p in sorted(artifact_dir.rglob("*")):
        if not p.is_file():
            continue
        resolved = str(p.resolve())
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        artifact = store.add_artifact(
            chat_id,
            p.name,
            resolved,
            "",
            message_id=message_id,
            origin=origin_for(resolved),
        )
        yield _artifact_payload(artifact)


def _tool_output_preview(value: Any) -> tuple[str, int]:
    try:
        raw = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        raw = str(value)
    return raw[:1200], len(raw)


def _run_tracked_tool(
    *,
    store: ChatStore,
    chat_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    call: Callable[[], Any],
    run_id: Optional[str] = None,
    emit: Optional[Callable[[str, Any], None]] = None,
) -> Any:
    def publish(event_type: str, data: dict[str, Any]) -> None:
        payload = {
            "tool_name": tool_name,
            "arguments": arguments if event_type == "local_tool_call_started" else {},
            "run_id": run_id,
            "agent": "orchestrator",
            **data,
        }
        record = store.add_event(chat_id, event_type, payload).model_dump()
        if emit is not None:
            emit("workflow_event", record)

    publish("local_tool_call_started", {})
    try:
        result = call()
    except Exception as exc:
        publish("local_tool_call_failed", {"message": str(exc)})
        raise
    preview, chars = _tool_output_preview(result)
    publish(
        "local_tool_call_finished",
        {"output_preview": preview, "output_chars": chars},
    )
    return result


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    built_index = _WEBUI_DIST / "index.html"
    if built_index.is_file():
        return HTMLResponse(built_index.read_text(encoding="utf-8"))
    return HTMLResponse((_WEBUI_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/skills")
def get_skills() -> list[dict[str, Any]]:
    return list_skills()


@app.get("/api/runtime-options")
def runtime_options() -> dict[str, Any]:
    settings = current_settings()
    return {
        "provider": settings.provider,
        "providers": ["openai", "anthropic"],
        "models": {
            # High-effort models first, then the fast/cheap tier so the UI can
            # offer a lower-cost option (also handy for cheap smoke testing).
            "openai": _dedupe(
                [*settings.high_effort_models_for_provider("openai"), settings.openai_fast_model]
            ),
            "anthropic": _dedupe(
                [*settings.high_effort_models_for_provider("anthropic"), settings.anthropic_fast_model]
            ),
        },
        "reasoning_efforts": ["none", "low", "medium", "high", "xhigh", "max"],
        "defaults": _store().default_settings().model_dump(),
        "skills": list_skills(),
        # Per-model context windows (input tokens) so the sidebar Context ring
        # knows the denominator for the fill percentage. Reuses the same table
        # the within-chat compaction logic uses (single source of truth).
        "context_windows": dict(_context_windows()),
        "default_context_window": _default_context_window(),
    }


def _context_windows() -> dict[str, int]:
    from .context import _WINDOWS
    return _WINDOWS


def _default_context_window() -> int:
    from .context import _DEFAULT_WINDOW
    return _DEFAULT_WINDOW


@app.get("/api/usage")
def usage_summary(month: Optional[str] = None) -> dict[str, Any]:
    """Monthly cost-center summary (tokens, cached tokens, estimated cost)."""
    from .usage import month_summary

    settings = current_settings()
    return month_summary(month, state_dir=settings.state_dir)


async def _save_chat_upload(chat_id: str, file: UploadFile) -> dict[str, str]:
    store = _store()
    try:
        store.get_chat(chat_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Chat not found")
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename")
    upload_id = uuid.uuid4().hex[:12]
    dest_dir = _chat_uploads_dir(chat_id) / upload_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _safe_upload_filename(file.filename)
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"id": upload_id, "chat_id": chat_id, "filename": dest.name, "path": str(dest.resolve())}


@app.post("/api/chats/{chat_id}/upload")
async def upload_chat_file(chat_id: str, file: UploadFile = File(...)) -> dict[str, str]:
    return await _save_chat_upload(chat_id, file)


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), chat_id: Optional[str] = Form(None)) -> dict[str, str]:
    if not chat_id:
        raise HTTPException(status_code=400, detail="Uploads must be associated with a chat")
    return await _save_chat_upload(chat_id, file)


def _artifact_file_response(store: ChatStore, chat_id: str, artifact_id: str) -> FileResponse:
    try:
        artifact = store.get_artifact(artifact_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if artifact.chat_id != chat_id or not _is_chat_artifact_path(artifact.path, chat_id):
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(Path(artifact.path).resolve(), filename=artifact.filename)


@app.get("/api/chats/{chat_id}/artifacts/{artifact_id}")
def download_chat_artifact(chat_id: str, artifact_id: str) -> FileResponse:
    return _artifact_file_response(_store(), chat_id, artifact_id)


@app.get("/api/artifacts/{name:path}")
def download_artifact(name: str) -> FileResponse:
    # Legacy compatibility: only chat-scoped artifacts may be resolved here.
    parts = Path(name).parts
    if len(parts) == 3 and parts[0] == "chat_artifacts":
        chat_id, filename = parts[1], parts[2]
        store = _store()
        for artifact in _list_visible_artifacts(store, chat_id):
            if artifact.filename == filename:
                return _artifact_file_response(store, chat_id, artifact.id)
    raise HTTPException(status_code=404, detail="Artifact not found")


@app.get("/api/projects")
def list_projects(include_archived: bool = False) -> list[dict[str, Any]]:
    return [p.model_dump() for p in _project_store().list_projects(include_archived=include_archived)]


@app.post("/api/projects")
def create_project(req: ProjectCreateRequest) -> dict[str, Any]:
    p = _project_store().create_project(
        req.name, slug=req.slug, brief=req.brief,
        jurisdiction=req.jurisdiction, domain_packs=req.domain_packs or None,
    )
    return p.model_dump()


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    store = _project_store()
    project = store.resolve(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return {
        "project": project.model_dump(),
        "memory": [m.model_dump() for m in store.list_memory(project.id)],
        "chats": store.list_project_chats(project.id),
        "context_block": store.build_context_block(project.id),
    }


@app.patch("/api/projects/{project_id}")
def patch_project(project_id: str, req: ProjectPatchRequest) -> dict[str, Any]:
    store = _project_store()
    project = store.resolve(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    updated = store.update_project(
        project.id, name=req.name, brief=req.brief, summary=req.summary,
        jurisdiction=req.jurisdiction, domain_packs=req.domain_packs, status=req.status,
    )
    return updated.model_dump()


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str) -> Response:
    store = _project_store()
    project = store.resolve(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    store.delete_project(project.id)
    return Response(status_code=204)


@app.post("/api/projects/{project_id}/memory")
def add_project_memory(project_id: str, req: ProjectMemoryRequest) -> dict[str, Any]:
    store = _project_store()
    project = store.resolve(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    mem = store.add_memory(
        project.id, req.kind, req.title, req.body, salience=req.salience, status=req.status,
    )
    return mem.model_dump()


@app.post("/api/projects/{project_id}/compact")
def compact_project(project_id: str) -> dict[str, Any]:
    store = _project_store()
    project = store.resolve(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return store.compact(project.id).model_dump()


@app.post("/api/chats/{chat_id}/project")
def assign_chat_project(chat_id: str, req: ChatProjectAssignRequest) -> dict[str, Any]:
    chat_store = _store()
    try:
        chat_store.get_chat(chat_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Chat not found")
    store = _project_store()
    project_id = None
    if req.project_id:
        project = store.resolve(req.project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        project_id = project.id
    store.assign_chat(chat_id, project_id)
    return {"chat_id": chat_id, "project_id": project_id}


@app.get("/api/chats")
def list_chats(include_archived: bool = False) -> list[dict[str, Any]]:
    return [c.model_dump() for c in _store().list_chats(include_archived=include_archived)]


@app.post("/api/chats")
def create_chat(req: ChatCreateRequest) -> dict[str, Any]:
    chat = _store().create_chat(title=req.title or "New chat", settings=req.settings)
    return chat.model_dump()


@app.get("/api/chats/{chat_id}")
def get_chat(chat_id: str) -> dict[str, Any]:
    store = _store()
    try:
        chat = store.get_chat(chat_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {
        "chat": chat.model_dump(),
        "messages": [m.model_dump() for m in store.list_messages(chat_id)],
        "events": [e.model_dump() for e in store.list_events(chat_id)],
        "artifacts": [_artifact_payload(a) for a in _list_visible_artifacts(store, chat_id)],
        "active_runs": [r.model_dump() for r in store.list_active_runs(chat_id)],
    }


@app.patch("/api/chats/{chat_id}")
def patch_chat(chat_id: str, req: ChatPatchRequest) -> dict[str, Any]:
    try:
        chat = _store().update_chat(
            chat_id,
            title=req.title,
            archived=req.archived,
            settings=req.settings,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat.model_dump()


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str) -> Response:
    store = _store()
    if store.has_active_run(chat_id):
        raise HTTPException(status_code=409, detail="Cannot delete a chat while a response is running")
    store.delete_chat(chat_id)
    return Response(status_code=204)


@app.post("/api/chats/{chat_id}/runs/cancel")
def cancel_chat_runs(chat_id: str) -> dict[str, Any]:
    store = _store()
    try:
        store.get_chat(chat_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Chat not found")

    cancelled: list[dict[str, Any]] = []
    for run in store.list_active_runs(chat_id):
        _cancel_run_event(run.id)
        cancelled.append(_mark_run_cancelled(store, run.id))
    return {"cancelled": cancelled, "count": len(cancelled)}


@app.post("/api/chats/{chat_id}/runs/{run_id}/cancel")
def cancel_chat_run(chat_id: str, run_id: str) -> dict[str, Any]:
    store = _store()
    try:
        run = store.get_run(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.chat_id != chat_id:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "running":
        return {"cancelled": [], "count": 0}
    _cancel_run_event(run.id)
    return {"cancelled": [_mark_run_cancelled(store, run.id)], "count": 1}


@app.get("/api/chats/{chat_id}/runs/{run_id}/stream")
def reconnect_run_stream(chat_id: str, run_id: str) -> StreamingResponse:
    """Attach a late subscriber to an in-progress run and replay its events.

    Used by the frontend transport's `reconnectToStream` to resume watching a
    run that is still producing (e.g. after a page reload, or a run started in
    another tab). Backfills already-persisted events for this run, then streams
    live events via the same pub/sub queue used by the primary turn endpoint.
    """
    store = _store()
    try:
        run = store.get_run(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.chat_id != chat_id:
        raise HTTPException(status_code=404, detail="Run not found")

    # If the run already finished, there is nothing live to resume.
    if run.status != "running":
        raise HTTPException(status_code=409, detail="Run is not active")

    # Subscribe BEFORE reading persisted history so we don't miss events that
    # land between the backfill read and the live attach.
    q = _subscribe_run(run_id)

    def gen():
        try:
            # Backfill: replay persisted events for this run so the reconnecting
            # client reconstructs prior progress. message_saved for the
            # assistant anchors the message id.
            try:
                assistant_message_id = getattr(run, "assistant_message_id", None)
                if assistant_message_id:
                    assistant_msg = store.get_message(assistant_message_id)
                    yield _sse("message_saved", assistant_msg.model_dump())
            except Exception:
                pass
            for ev in store.list_events(chat_id):
                if ev.run_id and ev.run_id != run_id:
                    continue
                yield _sse("workflow_event", ev.model_dump())
            # Live tail.
            while True:
                try:
                    item = q.get(timeout=10)
                except queue.Empty:
                    yield _sse("heartbeat", {"status": "working", "run_id": run_id})
                    continue
                if item is None:
                    break
                event, data = item
                yield _sse(event, data)
        finally:
            _unsubscribe_run(run_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/chats/{chat_id}/messages/{message_id}/export")
def export_message(chat_id: str, message_id: str, req: ExportRequest) -> dict[str, Any]:
    store = _store()
    try:
        chat = store.get_chat(chat_id)
        msg = store.get_message(message_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg.chat_id != chat_id:
        raise HTTPException(status_code=404, detail="Message not found")
    artifact_format = (req.format or "pdf").lower().strip(".")
    if artifact_format not in {"pdf", "docx"}:
        raise HTTPException(status_code=400, detail="Export format must be pdf or docx")
    export_settings = _settings_for_chat(chat.settings, chat_id=chat_id)
    # Clamp the watermark to a short banner so a malformed request cannot bloat
    # the header; empty string → no watermark.
    watermark = (req.watermark or "").strip()[:80] or None
    with override_current_settings(export_settings):
        artifact = _run_tracked_tool(
            store=store,
            chat_id=chat_id,
            tool_name=f"write_{artifact_format}",
            arguments={
                "title": req.title or "Legal Analysis",
                "source_message_id": message_id,
                "watermark": watermark,
            },
            call=lambda: _write_followup_artifact(
                export_settings,
                msg.content,
                req.title or "Legal Analysis",
                artifact_format,
                watermark=watermark,
            ),
        )
    rec = store.add_artifact(chat_id, artifact["filename"], artifact["path"], "", message_id=message_id)
    return _artifact_payload(rec)


def _stream_chat_turn(chat_id: str, req: ChatStreamRequest) -> StreamingResponse:
    store = _store()
    try:
        chat = store.get_chat(chat_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Chat not found")
    if store.has_active_run(chat_id):
        raise HTTPException(status_code=409, detail="A response is already running for this chat")
    attachment_paths = [_validated_attachment_path(chat_id, p) for p in req.attachments]

    settings_model = req.settings or chat.settings
    if req.skill_hint:
        settings_model.skill_hint = req.skill_hint
    store.update_chat(chat_id, settings=settings_model)
    user_msg = store.add_message(chat_id, "user", req.message, {"attachments": req.attachments})
    assistant_msg = store.add_message(
        chat_id,
        "assistant",
        "",
        {"run_status": "running", "provider": settings_model.provider, "model": settings_model.model},
    )
    run = store.create_run(chat_id, user_msg.id, assistant_msg.id)
    store.merge_message_metadata(assistant_msg.id, {"run_id": run.id})
    q = _subscribe_run(run.id)
    cancel_event = _register_run_cancel_event(run.id)

    def emit(event: str, data: Any) -> None:
        _publish_run(run.id, event, data)

    def is_cancelled() -> bool:
        return cancel_event.is_set()

    def persist_assistant(content: str, metadata: Optional[dict[str, Any]] = None) -> None:
        if is_cancelled():
            return
        current = store.get_message(assistant_msg.id)
        merged = {**current.metadata, **(metadata or {})}
        store.update_message(assistant_msg.id, content, merged)

    def finish_cancelled() -> None:
        try:
            current = store.get_run(run.id)
        except KeyError:
            return
        if current.status == "running":
            _mark_run_cancelled(store, run.id)

    def produce() -> None:
        token = workflow_event_sink_var.set(
            lambda record: emit(
                "workflow_event",
                store.add_event(
                    chat_id,
                    record.get("event_type", "workflow_event"),
                    record,
                ).model_dump(),
            )
        )
        assistant_text = ""
        run_finished = False
        run_settings = _settings_for_chat(settings_model, chat_id=chat_id)
        active_project_id = _project_store().project_id_for_chat(chat_id)
        seen_paths = store.existing_artifact_paths(chat_id)
        last_persisted_at = 0.0
        try:
            with override_current_settings(run_settings), active_project(active_project_id), usage_run_context(
                chat_id=chat_id, run_id=run.id
            ):
                emit("message_saved", user_msg.model_dump())
                emit("message_saved", store.get_message(assistant_msg.id).model_dump())

                if is_cancelled():
                    finish_cancelled()
                    run_finished = True
                    return

                executor = WorkflowExecutor(settings=run_settings)
                recent = [m for m in store.list_messages(chat_id, limit=12) if m.id != assistant_msg.id]
                attachments = _chat_context_attachment_paths(
                    store,
                    chat_id,
                    current_paths=attachment_paths,
                )
                for ev in executor.stream(
                    req.message,
                    attachments=attachments,
                    recent_messages=recent,
                    memory_summary=chat.memory_summary,
                    skill_hint=settings_model.skill_hint,
                    cancel_event=cancel_event,
                ):
                    if is_cancelled():
                        finish_cancelled()
                        run_finished = True
                        return
                    if ev.kind in {"delta", "agent_delta", "reasoning_delta"}:
                        text = ev.data.get("text", "")
                        if ev.kind == "delta":
                            assistant_text += text
                            now = time.monotonic()
                            if now - last_persisted_at > 0.5:
                                persist_assistant(assistant_text, {"run_status": "running"})
                                last_persisted_at = now
                        # agent_delta + reasoning_delta are transient previews:
                        # forwarded to the client, never accumulated or persisted.
                        emit(ev.kind, ev.data)
                    elif ev.kind == "done":
                        assistant_text = ev.data.get("text", assistant_text)
                        # Per-turn usage + wall-clock (B3). Emit AND persist as a
                        # replayable event so the sidebar Context ring, run header,
                        # and duration chip survive reload (live ≡ rehydrated).
                        usage = ev.data.get("usage") or {}
                        duration_ms = ev.data.get("duration_ms")
                        if usage or duration_ms is not None:
                            def _u(key: str) -> int:
                                return int(usage.get(key) or 0)
                            input_tok = _u("input_tokens")
                            cache_read = _u("cache_read_input_tokens")
                            cache_write = _u("cache_creation_input_tokens")
                            # Context-window fill = prompt tokens actually sent.
                            # Anthropic input_tokens EXCLUDES cache (add them);
                            # OpenAI includes cache and reports 0 cache buckets,
                            # so the sum collapses to input_tokens. One formula,
                            # both providers.
                            context_tokens = input_tok + cache_read + cache_write
                            turn_usage_payload = {
                                "input_tokens": input_tok,
                                "output_tokens": _u("output_tokens"),
                                "cache_read_tokens": cache_read,
                                "cache_write_tokens": cache_write,
                                "context_tokens": context_tokens,
                                "duration_ms": duration_ms,
                                "provider": run_settings.provider,
                                "model": run_settings.model_for_provider(),
                            }
                            store.add_event(chat_id, "turn_usage", turn_usage_payload)
                            emit("turn_usage", turn_usage_payload)
                        persist_assistant(
                            assistant_text,
                            {
                                "citation_audit": ev.data.get("citation_audit"),
                                "provider": run_settings.provider,
                                "model": run_settings.model_for_provider(),
                                "usage": usage or None,
                                "duration_ms": duration_ms,
                                "run_status": "complete",
                            },
                        )
                        for artifact in _scan_new_artifacts(
                            store,
                            chat_id,
                            seen_paths,
                            run_settings.outputs_dir,
                            assistant_msg.id,
                        ):
                            emit("artifact", artifact)
                        store.update_run(run.id, status="complete", finished=True)
                        run_finished = True
                        emit("done", {"text": assistant_text, "message_id": assistant_msg.id})
                        # Within-chat context management: roll the memory summary
                        # forward. When the full transcript approaches the model's
                        # window, widen the lookback so older turns dropping out of
                        # the per-turn digest are folded into the summary (lossless
                        # compaction) and surface the event for observability.
                        from .context import estimate_tokens, should_compact

                        all_messages = store.list_messages(chat_id)
                        compacting = should_compact(
                            all_messages,
                            run_settings,
                            threshold=run_settings.chat_compaction_threshold,
                            summary_tokens=estimate_tokens(chat.memory_summary),
                        )
                        lookback = 40 if compacting else 18
                        messages = store.list_messages(chat_id, limit=lookback)
                        if compacting:
                            emit(
                                "context_compaction",
                                {"reason": "transcript_near_window", "lookback": lookback,
                                 "transcript_messages": len(all_messages)},
                            )
                        memory_token = workflow_event_sink_var.set(None)
                        try:
                            summary = summarize_with_fast_model(run_settings, messages, chat.memory_summary)
                        finally:
                            workflow_event_sink_var.reset(memory_token)
                        store.update_chat(chat_id, memory_summary=summary)
                        emit("memory_updated", {"summary": summary})
                        _maybe_generate_chat_title(store, chat_id, run_settings, emit)
                    elif ev.kind == "workflow_plan":
                        store.add_event(chat_id, "workflow_plan", ev.data)
                        emit("workflow_plan", ev.data)
                    elif ev.kind == "citation_audit":
                        store.add_event(chat_id, "citation_audit", ev.data)
                        emit("citation_audit", ev.data)
                    elif ev.kind == "cancelled":
                        finish_cancelled()
                        run_finished = True
                        return
                    elif ev.kind == "error":
                        message = ev.data.get("message", "")
                        text = OUT_OF_MONEY_MESSAGE if ev.data.get("out_of_money") else message
                        persist_assistant(text, {"run_status": "error", **ev.data})
                        store.update_run(run.id, status="error", error=text, finished=True)
                        run_finished = True
                        store.add_event(chat_id, "error", {"message": text, **ev.data})
                        emit("error", {"message": text, **ev.data})
                    else:
                        store.add_event(chat_id, ev.kind, ev.data)
                        emit(ev.kind, ev.data)
        except Exception as e:
            if is_cancelled():
                finish_cancelled()
                run_finished = True
                return
            message = OUT_OF_MONEY_MESSAGE if is_out_of_money_error(e) else str(e)
            persist_assistant(
                message,
                {"run_status": "error", "out_of_money": message == OUT_OF_MONEY_MESSAGE},
            )
            store.update_run(run.id, status="error", error=message, finished=True)
            run_finished = True
            store.add_event(chat_id, "error", {"message": message, "out_of_money": message == OUT_OF_MONEY_MESSAGE})
            emit("error", {"message": message, "out_of_money": message == OUT_OF_MONEY_MESSAGE})
        finally:
            if is_cancelled():
                finish_cancelled()
                run_finished = True
            if not run_finished:
                persist_assistant(assistant_text, {"run_status": "error"})
                store.update_run(run.id, status="error", error="Workflow ended before completion.", finished=True)
            workflow_event_sink_var.reset(token)
            _finish_run_streams(run.id)
            _unregister_run_cancel_event(run.id)

    def gen():
        try:
            while True:
                try:
                    item = q.get(timeout=10)
                except queue.Empty:
                    yield _sse("heartbeat", {"status": "working", "run_id": run.id})
                    continue
                if item is None:
                    break
                event, data = item
                yield _sse(event, data)
        finally:
            _unsubscribe_run(run.id, q)

    thread = threading.Thread(target=produce, daemon=True)
    thread.start()
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/chats/{chat_id}/messages/stream")
def stream_chat_message(chat_id: str, req: ChatStreamRequest) -> StreamingResponse:
    return _stream_chat_turn(chat_id, req)


@app.post("/api/chat")
def chat(req: ChatRequest) -> StreamingResponse:
    store = _store()
    chat_id = req.chat_id
    if chat_id is None:
        chat_id = store.create_chat(settings=req.settings).id
    return _stream_chat_turn(
        chat_id,
        ChatStreamRequest(
            message=req.message,
            attachments=req.attachments,
            skill_hint=req.skill_hint,
            settings=req.settings,
        ),
    )
