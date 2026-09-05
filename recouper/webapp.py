"""FastAPI entry point for the Recouper local operator dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .agent.gemini import GeminiPlanner
from .product import DashboardService


WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


def _live_planner() -> Optional[str]:
    """Which model, if any, this deployment can call for an on-demand plan.

    Reported so the UI can hide the button rather than offer an action that
    will fail -- and so a reader can tell at a glance whether the rationales
    on screen came from a model or from the fallback table.
    """
    planner = GeminiPlanner()
    return planner.model if planner.available else None


def create_app(service: Optional[DashboardService] = None) -> FastAPI:
    dashboard = service or DashboardService()
    app = FastAPI(
        title="Recouper Operator Dashboard",
        description="A simulator-backed, policy-gated revenue recovery console.",
        version="1.0.0",
    )

    app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        # No-store: the dashboard is a local tool and a stale shell showing
        # yesterday's run state would be worse than a reload.
        return FileResponse(
            WEB_ROOT / "index.html", headers={"Cache-Control": "no-store"}
        )

    @app.get("/api/health")
    def health() -> dict:
        return {
            "ok": True,
            "service": "recouper",
            "mode": "simulator",
            "message": "No real payment or notification is sent",
        }

    @app.get("/api/meta")
    def meta() -> dict:
        return {
            "product": "Recouper",
            "mode": "simulator",
            "actions": [
                "retry_payment",
                "create_payment_link",
                "send_reminder",
                "escalate_to_human",
                "close_no_action",
            ],
            "decisions": ["approve", "reject", "escalate"],
            "policy": "All approved work is re-gated by deterministic policy rules",
            "live_planner": _live_planner(),
        }

    @app.get("/api/runs")
    def list_runs() -> list[dict]:
        return dashboard.list_runs()

    @app.post("/api/runs")
    def create_run(body: Optional[dict] = None) -> dict:
        body = body or {}
        try:
            session = dashboard.create_run(
                seed=int(body.get("seed", 42)),
                control_fraction=float(body.get("control_fraction", 0.20)),
                faults=bool(body.get("faults", False)),
                no_llm=bool(body.get("no_llm", True)),
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return dashboard.get_run(session.run_id)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        try:
            return dashboard.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.get("/api/runs/{run_id}/cases")
    def get_cases(
        run_id: str,
        status: Optional[str] = None,
        arm: Optional[str] = None,
        q: Optional[str] = None,
    ) -> list[dict]:
        try:
            return dashboard.get_cases(run_id, status=status, arm=arm, query=q)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.get("/api/runs/{run_id}/cases/{case_id}")
    def get_case(run_id: str, case_id: str) -> dict:
        try:
            return dashboard.get_case(run_id, case_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="case not found") from exc

    @app.get("/api/runs/{run_id}/audit")
    def get_audit(run_id: str, case_id: Optional[str] = None) -> list[dict]:
        try:
            return dashboard.audit(run_id, case_id=case_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.get("/api/runs/{run_id}/verify")
    def verify(run_id: str) -> dict:
        try:
            return dashboard.verify(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.post("/api/runs/{run_id}/approve-safe")
    def approve_safe(run_id: str) -> dict:
        try:
            dashboard.approve_safe(run_id)
            return dashboard.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/cases/{case_id}/decision")
    def decide_case(run_id: str, case_id: str, body: Optional[dict] = None) -> dict:
        body = body or {}
        try:
            dashboard.decide_case(
                run_id,
                case_id,
                str(body.get("decision", "")),
                str(body.get("note", "")),
            )
            return dashboard.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run or case not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/cases/{case_id}/replan")
    def replan_case(run_id: str, case_id: str) -> dict:
        """Ask the live model to re-plan one case, on demand."""
        try:
            return dashboard.replan_case(run_id, case_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run or case not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str) -> dict:
        try:
            dashboard.resume(run_id)
            return dashboard.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
