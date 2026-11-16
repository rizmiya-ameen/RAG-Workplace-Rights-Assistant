from fastapi import FastAPI

app = FastAPI(
    title="Workplace Rights Assistant",
    description="General information about Australian Modern Awards, the NES and WorkSafe "
    "Victoria guidance. Not legal advice.",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
