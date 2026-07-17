import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-key-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'chess.db')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH", "stockfish")
