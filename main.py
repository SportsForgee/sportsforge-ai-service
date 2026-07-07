from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List

from video_analysis.routes import router as video_analysis_router

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(video_analysis_router)


class Player(BaseModel):
    name: str
    position: str
    performance: float
    fitness: float
    injuryRisk: float
    status: str


class InsightResponse(BaseModel):
    message: str
    priority: str  # "high" | "medium" | "low"


@app.get("/")
def home():
    return {"message": "AI Service Running"}


@app.post("/insights", response_model=List[InsightResponse])
def generate_insights(players: List[Player]):
    insights: List[InsightResponse] = []

    # --- High-risk injury alerts ---
    high_risk = [p for p in players if p.injuryRisk >= 40]
    for p in high_risk:
        insights.append(InsightResponse(
            message=f"{p.name} has a {p.injuryRisk:.0f}% injury risk — consider reducing training load immediately.",
            priority="high",
        ))

    # --- Low fitness warnings ---
    low_fitness = [p for p in players if p.fitness < 75]
    for p in low_fitness:
        insights.append(InsightResponse(
            message=f"{p.name}'s fitness is at {p.fitness:.0f}% — schedule a recovery or conditioning session.",
            priority="high" if p.fitness < 65 else "medium",
        ))

    # --- Performance/fitness mismatch (high performance, low fitness = overextension risk) ---
    overextended = [p for p in players if p.performance >= 85 and p.fitness < 80]
    for p in overextended:
        insights.append(InsightResponse(
            message=f"{p.name} is performing at {p.performance:.0f} but fitness is only {p.fitness:.0f}% — monitor for overextension.",
            priority="medium",
        ))

    # --- Moderate injury risk ---
    moderate_risk = [p for p in players if 20 <= p.injuryRisk < 40]
    for p in moderate_risk:
        insights.append(InsightResponse(
            message=f"{p.name} shows a moderate injury risk ({p.injuryRisk:.0f}%) — keep training intensity in check.",
            priority="medium",
        ))

    # --- Caution status players ---
    caution_players = [p for p in players if p.status == "Caution"]
    for p in caution_players:
        if not any(p.name in i.message for i in insights):
            insights.append(InsightResponse(
                message=f"{p.name} is flagged as Caution — review recent session data before next match.",
                priority="medium",
            ))

    # --- Top performers positive note ---
    top = sorted(players, key=lambda p: p.performance, reverse=True)[:2]
    for p in top:
        if p.fitness >= 85 and p.injuryRisk < 15:
            insights.append(InsightResponse(
                message=f"{p.name} is in peak condition (performance {p.performance:.0f}, fitness {p.fitness:.0f}%) — consider increased responsibility.",
                priority="low",
            ))

    # --- Team-level summary ---
    avg_fitness = sum(p.fitness for p in players) / len(players) if players else 0
    match_ready_count = sum(1 for p in players if p.status == "Match Ready")
    if avg_fitness >= 85:
        insights.append(InsightResponse(
            message=f"Team average fitness is strong at {avg_fitness:.0f}% — {match_ready_count}/{len(players)} players are match ready.",
            priority="low",
        ))
    elif avg_fitness < 75:
        insights.append(InsightResponse(
            message=f"Team average fitness is low at {avg_fitness:.0f}% — consider a recovery week before next fixture.",
            priority="high",
        ))

    # Deduplicate by message and cap at 6
    seen = set()
    unique: List[InsightResponse] = []
    for i in insights:
        if i.message not in seen:
            seen.add(i.message)
            unique.append(i)

    # Sort: high first, then medium, then low
    order = {"high": 0, "medium": 1, "low": 2}
    unique.sort(key=lambda i: order.get(i.priority, 3))

    return unique[:6]

